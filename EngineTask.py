
import numpy
import sys
import threading
import time
import theano
from EngineUtil import assign_dev_data
from Log import log
from Util import hms, progress_bar, terminal_size, hdf5_strings, interrupt_main
from Device import Device
from TaskSystem import ProcConnectionDied


class TaskThread(threading.Thread):
    def __init__(self, task, network, devices, data, batches, eval_batch_size=0, start_batch=0, pad_batches=False, share_batches = False, report_prefix=None, exclude=None):
      """
      :type task: str
      :type network: Network.LayerNetwork
      :type devices: list[Device.Device]
      :type data: Dataset.Dataset
      :type batches: EngineBatch.BatchSetGenerator
      :type start_batch: int
      :type pad_batches: bool
      :param str report_prefix: such as epoch or so. only for reporting
      """
      threading.Thread.__init__(self, name="TaskThread %s" % task)
      if eval_batch_size == 0:
        eval_batch_size = sys.maxint
      self.share_batches = share_batches
      self.eval_batch_size = eval_batch_size
      self.eval_batch_idx = 0
      self.start_batch = start_batch
      self.pad_batches = pad_batches
      self.devices = devices
      self.network = network
      self.batches = batches
      self.exclude = exclude
      self.task = task
      self.data = data
      self.daemon = True
      self.elapsed = 0
      self.finalized = False
      self.score = None
      self.num_frames = 0
      self.batch_idx = None; " :type: int | None "
      self.device_crash_batch = None; " :type: int | None "
      self.report_prefix = report_prefix or self.task
      self.lock = threading.Lock()
      self.start()

    def assign_dev_data(self, device, batches):
      return assign_dev_data(device, self.data, batches, self.network.recurrent, self.pad_batches, self.exclude)

    def allocate_device(self, device):
      batches = self.batches.peek_next_n(device.num_batches)
      success, batch_adv_idx = self.assign_dev_data(device, batches)
      if not success: return []
      self.batches.advance(batch_adv_idx)
      return batches

    def allocate_devices(self, selected_devices = None):
      """
      Sets the device data, i.e. the next batches, via self.batches.
      This calls Dataset.load_seqs() to get the data.
      This sets:
        device.data
        device.targets
        device.ctc_targets
        device.tags
        device.index
      :rtype: list[list[EngineBatch.Batch]]
      :returns list of batches per device
      """
      if not selected_devices:
        selected_devices = self.devices
      devices_batches = []; " :type: list[list[EngineBatch.Batch]] "
      if self.share_batches:
        batches = self.batches.peek_next_n(1)
      for device in selected_devices:
        if not self.share_batches:
          batches = self.batches.peek_next_n(device.num_batches)
        success, batch_adv_idx = self.assign_dev_data(device, batches)
        batch_idx = self.batches.get_current_batch_idx()
        assert success, "batches %s with seqs at %i failed to load" % \
                        (range(batch_idx, batch_idx + batch_adv_idx), batches[batch_adv_idx - 1].start_seq)
        devices_batches.append(batches)
        if not self.share_batches:
          self.batches.advance(batch_adv_idx)
      if self.share_batches:
        self.batches.advance(batch_adv_idx)
      return devices_batches

    def prepare_device_for_batch(self, device):
      """ :type device: Device.Device """
      pass
    def get_device_prepare_args(self):
      return {"network": self.network, "updater": None}
    def evaluate(self, batchess, results, result_format, num_frames):
      """
      :param list[list[EngineBatch.Batch]] batchess: batches per device
      :param list[list[numpy.ndarray]] results: results per device
      :param list[str]|None result_format: describes what we have in a result list
      :type num_frames: int
      """
      pass
    def initialize(self):
      pass
    def finalize(self):
      self.finalized = True

    class DeviceBatchRun(threading.Thread):
      def __init__(self, parent, devices):
        """
        :type parent: TaskThread
        """
        threading.Thread.__init__(self, name="DeviceThread %s" % " ".join([dev.name for dev in devices]))
        self.alloc_devices = devices
        self.parent = parent
        self.run_start_batch_idx = parent.batches.get_current_batch_idx()
        self.allocated = False
        self.processing = False
        self.finished = True
        self.crashed = False
        self.num_frames = 0
        self.run_frames = 0
        self.daemon = True
        self.active = True
        self.result = { 'batchess': [], 'results': [], 'result_format': None, 'num_frames': 0 }
        if self.alloc_devices:
          self.start()

      def allocate(self):
        self.devices_batches = self.parent.allocate_devices(self.alloc_devices)
        self.run_frames = 0
        for batches in self.devices_batches:
          assert batches
          assert batches[0].seqs
          assert batches[0].seqs[0].frame_length[1] > 0
          self.run_frames += sum([batch.get_total_num_frames() for batch in batches])
        if self.parent.share_batches:
          self.run_frames /= len(self.alloc_devices)
        assert self.run_frames > 0
        self.allocated = True

      def finish(self):
        """
        :returns whether everything is fine.
        """
        device_results, outputs_format = self.device_collect_results()
        if device_results is None:
          if not getattr(sys, "exited", False):
            print >> log.v3, "device crashed on batch", self.run_start_batch_idx
          self.parent.device_crash_batch = self.run_start_batch_idx
          self.crashed = True
          return False
        assert len(device_results) == len(self.alloc_devices) == len(self.devices_batches)

        if outputs_format and "gparams..." in outputs_format:
          output_results = []
          for i in xrange(len(self.alloc_devices)):
            res = Device.make_result_dict(device_results[i], outputs_format)
            if "gparams" in res:
              self.alloc_devices[i].sync_net_train_params()
              devnet = self.alloc_devices[i].get_net_train_params(self.parent.network)
              vars = self.parent.network.get_all_params_vars()
              for p, q in zip(vars, devnet):
                p.set_value(q)
              gparams = {}
              for k in self.parent.network.cost:
                gparams[k] = {}
                for p in vars:
                  gparams[k][p] = numpy.zeros(p.get_value(borrow=True, return_internal_type=True).shape, dtype=theano.config.floatX)
              res_gparams = res["gparams"]
              for i,k in enumerate(self.parent.network.cost):
                for p, q in zip(vars, res_gparams): # TODO #[i * len(self.parent.network.train_params_vars):(i+1) * len(self.parent.network.train_params_vars)]):
                  if q.shape == p.get_value().shape:
                    gparams[k][p] = q
                  elif q.shape:
                    print >> log.v2, "warning: shape for gradient does not match:", p.get_value().shape, q.shape
              self.parent.updater.setNetParamDeltas(gparams)
              self.parent.updater.update()
              self.alloc_devices[i].set_net_params(self.parent.network)
              res.pop('gparams', None)
            output_results.append([res[k] for k in res if k != 'gparams'])
          outputs_format = res.keys()
        else:
          output_results = device_results

        self.result = { 'batchess': self.devices_batches, 'results': output_results, 'result_format': outputs_format, 'num_frames': self.num_frames }
        self.parent.lock.acquire()
        self.print_process()
        self.parent.lock.release()
        return True

      def run(self):
        try:
          while self.active:
            if self.allocated and not self.finished:
              self.device_run()
              self.num_frames = self.run_frames
              self.processing = True
              self.allocated = False
              self.finish()
              self.finished = True
              self.processing = False
            else:
              time.sleep(0.01)
        except Exception:
          self.crashed = True
          self.finished = True
          sys.excepthook(*sys.exc_info())

      def stop(self):
        self.active = False

      def device_run(self):
        batch_idx = self.run_start_batch_idx
        assert len(self.alloc_devices) == len(self.devices_batches)
        for device, batches in zip(self.alloc_devices, self.devices_batches):
          if self.parent.network.recurrent:
            print >> log.v5, "running", device.data.shape[1], \
                             "sequence slices (%i nts)" % (device.data.shape[0] * device.data.shape[1]),
          else:
            print >> log.v5, "running", device.data.shape[0] * device.data.shape[1], "frames",
          if device.num_batches == 1:
            print >> log.v5, "of batch %i" % batch_idx,
          else:
            print >> log.v5, "of batches %i-%i" % (batch_idx, batch_idx + device.num_batches - 1),
          print >> log.v5, "on device", device.name
          device.run(self.parent.task)
      #if not self.share batch_idx += device.num_batches

      def device_collect_results(self):
        device_results = []
        outputs_format = None
        for i, device in enumerate(self.alloc_devices):
          try:
            result, outputs_format_new = device.result()
          except RuntimeError:
            return None, None
          if result is None:
            return None, None
          assert isinstance(result, list)
          assert len(result) > 0  # we always expect to get some result
          if i >= 1:
            assert outputs_format == outputs_format_new, "We expect to always get the same output format."
          outputs_format = outputs_format_new
          device_results.append(result)
          device.tot_cost += result[0]
        return device_results, outputs_format

      def device_mem_usage_str(self, devices):
        """
        :type devices: list[Device.Device]
        :rtype: str | None
        """
        if not devices:
          return None
        mem_info = [device.get_memory_info() for device in devices]
        if len(mem_info) == 1 and mem_info[0] is None:
          return None
        mem_usage = [info.used if info else None for info in mem_info]
        s = ["%s MB" % (mem / (1024*1024)) if mem is not None else "unknown" for mem in mem_usage]
        return "/".join(s)

      def device_run_evaluate(self):
        """
        :param list[(float,params...)] results: result[i] is result for batch + i, result[i][0] is score
        :param list[str]|None result_format: describes what we have in a result list
        :type num_frames: int
        :rtype: dict[str]
        """
        results = self.result['results']
        assert results
        result_format = self.result['result_format']
        if not result_format:
          if len(results[0]) == 2:
            result_format = ["cost", "error"]  # default eval format
          else:
            return {}
        num_frames = self.result['num_frames']
        assert num_frames > 0
        num_frames *= len(self.alloc_devices)
        results = [Device.make_result_dict(res, result_format) for res in results]
        cost = [res["cost"] for res in results]
        score = sum(cost)
        score /= len(self.alloc_devices)
        eval_info = {"score": score / num_frames}
        # Maybe we got some more info such as gradient_norm.
        # See Device.initialize().
        for attrib in set(results[0].keys()).difference(["cost", "ctc_priors", "gparams"]):
          eval_info[attrib] = sum([res[attrib] for res in results]) / float(num_frames)
        return eval_info

      def print_process(self):
        if not self.parent.interactive and not log.v[5]:
          return
        start_elapsed = time.time() - self.parent.start_time
        complete = self.parent.batches.completed_frac()
        assert complete > 0
        total_time_estimated = start_elapsed / complete
        remaining_estimated = total_time_estimated - start_elapsed
        if log.verbose[5]:
          mem_usage = self.device_mem_usage_str(self.alloc_devices)
          info = [
            self.parent.report_prefix,
            "batch %i" % self.run_start_batch_idx]
          # Such as score.
          info += ["%s %s" % item for item in sorted(self.device_run_evaluate().items())]
          info += [
            "elapsed %s" % hms(start_elapsed),
            "exp. remaining %s" % hms(remaining_estimated),
            "complete %.02f%%" % (complete * 100)]
          if mem_usage:
            info += ["memory %s" % mem_usage]
          print >> log.v5, ", ".join(filter(None, info))
        if self.parent.interactive:
          progress_bar(complete, hms(remaining_estimated))

    def device_can_run_async(self):
      return False
      if len(self.devices) != 1:
        return False
      if self.devices[0].blocking:
        # If we are in the same proc (= blocking), nothing can be async.
        return False
      if self.devices[0].updater is None:
        # If nothing needs to be updated, we can run async.
        return True
      # We can run async iff we do the updates online.
      return self.devices[0].updater.updateOnDevice

    def run(self):
      # Wrap run_inner() for better exception printing.
      # Thread.__bootstrap_inner() ignores sys.excepthook.
      try:
        self.run_inner()
      except ProcConnectionDied:
        if not getattr(sys, "exited", False):
          # Normally we should have caught that in run_inner(), so somewhat unexpected.
          print >> log.v4, "%s. Some device proc crashed unexpectedly." % self
        # Just pass on. We have self.finalized == False which indicates the problem.
      except Exception:
        # Catch all standard exceptions.
        # These are not device errors. We should have caught them in the code
        # and we would leave self.finalized == False.
        # Don't catch KeyboardInterrupt here because that will get send by the main thread
        # when it is exiting. It's never by the user because SIGINT will always
        # trigger KeyboardInterrupt in the main thread only.
        try:
          print >> log.v1, "%s failed" % self.name
          if log.v[4]:
            sys.excepthook(*sys.exc_info())
            print ""
        finally:
          # Exceptions are fatal. If we can recover, we should handle it in run_inner().
          interrupt_main()

    def run_inner(self):
      self.start_time = time.time()
      for device in self.devices:
        device.prepare(**self.get_device_prepare_args())
      self.initialize()
      terminal_width, _ = terminal_size()
      self.interactive = (log.v[3] and terminal_width >= 0)
      print >> log.v5, "starting task", self.task

      canRunAsync = self.device_can_run_async()
      remainingDeviceRun = None; " :type: DeviceBatchRun "

      if canRunAsync:
        print >> log.v5, "Run %s in async mode." % self.name

      for device in self.devices:
        device.eval_batch_idx = -1
        device.start_epoch_stats()
        device.num_frames = 0
        device.tot_cost = 0
        device.tot = 0

      num_device_runs = 1 if self.share_batches else len(self.devices)
      deviceRuns = [ self.DeviceBatchRun(self, [self.devices[i]] if not self.share_batches else self.devices) for i in xrange(num_device_runs) ]

      results = { 'batchess': [], 'results': [], 'num_frames' : 0 }
      run_frames = 0

      crashed = False

      while True:
        if getattr(sys, "exited", False):
          # This happens when we exit Python.
          # Without this check, this thread would keep running until all exit handlers of Python are done.
          print >> log.v5, "%s stopped" % self
          crashed = True
          break

        for i in xrange(num_device_runs):
          if deviceRuns[i].crashed:
            crashed = True
            break
          if deviceRuns[i].finished:
            results['batchess'] += deviceRuns[i].result['batchess'][:]
            results['results'] += deviceRuns[i].result['results'][:]
            results['result_format'] = deviceRuns[i].result['result_format']
            deviceRuns[i].finished = False
        if crashed:
          break

        if run_frames >= self.eval_batch_size or not self.batches.has_more():
          if all(not (dev.finished or dev.allocated or dev.processing) for dev in deviceRuns):
            results['num_frames'] = run_frames
            self.num_frames += run_frames
            self.evaluate(**results)
            self.eval_batch_idx += 1
            run_frames = 0
            results['batchess'] = []
            results['results'] = []
            for device in self.devices:
              device.num_frames = 0
              device.tot_cost = 0
            if not self.batches.has_more():
              break
          else:
            time.sleep(0.01)

        match = True
        while self.batches.has_more() and run_frames < self.eval_batch_size and match:
          self.batch_idx = self.batches.get_current_batch_idx()
          if self.batch_idx < self.start_batch:
            self.batches.advance(1)
            break
          match = False
          for i in xrange(num_device_runs):
            if not deviceRuns[i].allocated:
              deviceRuns[i].allocate()
              run_frames += deviceRuns[i].run_frames
              match = True
              break
        if not match:
          time.sleep(0.01)

      for device in self.devices:
        device.finish_epoch_stats()
      if crashed: return
      self.finalize()
      if self.interactive: progress_bar()
      self.elapsed = (time.time() - self.start_time)


class ModelBrokenError(Exception):
  """
  We got a nan/inf at the result somewhere. This means that something is broken.
  """
  def __init__(self, msg, batches):
    """
    :type msg: str
    :type batches: list[EngineBatch.Batch]
    """
    assert len(batches) > 0
    msg = "%s Starting at seq %i." % (msg, batches[0].start_seq)
    super(ModelBrokenError, self).__init__(msg)
    self.batches = batches


class TrainTaskThread(TaskThread):
  def __init__(self, network, devices, data, batches, learning_rate, updater, **kwargs):
    """
    :type network: Network.LayerNetwork
    :type devices: list[Device.Device]
    :type data: Dataset.Dataset
    :type batches: EngineBatch.BatchSetGenerator
    :type learning_rate: float
    :type updater: Updater.Updater
    """
    self.updater = updater
    self.learning_rate = learning_rate
    self.do_ctc_priors = network.ctc_priors is not None
    self.ctc_priors = None
    super(TrainTaskThread, self).__init__("train", network, devices, data=data, batches=batches, **kwargs)

  def initialize(self):
    self.score = 0
    if self.do_ctc_priors:
      self.ctc_priors = numpy.zeros(shape=(self.network.n_out,), dtype=theano.config.floatX)
    for device in self.devices:
      device.set_learning_rate(self.learning_rate)
    if not self.updater.isInitialized:
      self.updater.initVars(self.network, None)
      self.updater.setLearningRate(self.learning_rate)

  def prepare_device_for_batch(self, device):
    """ :type device: Device.Device """
    return

  def get_device_prepare_args(self):
    kwargs = super(TrainTaskThread, self).get_device_prepare_args()
    kwargs["updater"] = self.updater
    kwargs["train_param_args"] = self.network.train_param_args
    return kwargs

  def save_ctc_priors(self, filename, epoch_str):
    assert self.ctc_priors is not None
    with open(filename, 'a') as f:
      print >> f, epoch_str
      numpy.savetxt(f, self.ctc_priors, newline=" ")
      print >> f

  class CopyManager():
    class CopyThread(threading.Thread):
      def __init__(self, device, network, copy_to_device):
        threading.Thread.__init__(self, name="CopyThread %s" % device.name)
        self.copy_to_device = copy_to_device
        self.device = device
        self.network = network
        self.active = True
        self.start()

      def run(self):
        if self.copy_to_device:
          self.device.set_net_params(self.network)
          self.result = True
        else:
          self.result = self.device.get_net_train_params(self.network)
        self.active = False

    def __init__(self, devices):
      self.devices = devices
      self.network = None

    def _copy(self, copy_to_device):
      threads = []
      for device in self.devices:
        threads.append(self.CopyThread(device, self.network, copy_to_device))
      result = []
      for thread in threads:
        if thread.active:
          thread.join()
        result.append(thread.result)
      return result

    def copy_to_device(self, network):
      self.network = network
      return self._copy(True)

    def copy_from_device(self):
      return self._copy(False)


  def create_consensus(self, cost, num_frames):
    for device in self.devices:
      device.sync_net_train_params()
    try:
      basenet = self.network.train_params_vars
      consnet = [numpy.zeros(p.get_value().shape, dtype='float32') for p in basenet]
      hypnets = []
      nparams = len(basenet)
      encoded = []
      #pipe = self.CopyManager(self.devices)
      #hypnets = pipe.copy_from_device()
      for device in self.devices:
        hypnets.append([ p for p in device.get_net_train_params(self.network) ])
      if len(hypnets) == 0:
        consnet = basenet
      elif len(hypnets) == 1:
        consnet = hypnets[0]
      else:
        # consensus via average
        for i in xrange(nparams):
          nframes = numpy.sum([ dev.num_frames for net,dev in zip(hypnets,self.devices) if numpy.sum(abs(net[i] - basenet[i].get_value())) > 0.0001 ])
          #ndevs = len([ dev for dev in self.devices if abs(numpy.sum(net[i] - basenet[i].get_value())) > 0.0001 ])
          #consnet[i] = basenet[i].get_value() + numpy.sum([(net[i] - basenet[i].get_value()) * (float(device.num_frames) / num_frames) for net,dev in zip(hypnets,self.devices) if basenet[i].layer.name in dev.update_specs['layers']], axis = 0)
          if nframes:
            consnet[i] = basenet[i].get_value() + numpy.sum([ (net[i] - basenet[i].get_value()) * (float(device.num_frames) / nframes) for net,dev in zip(hypnets,self.devices) ], axis = 0)
          else:
            print >> log.v4, "warning: no update available for parameter", basenet[i]
          #consnet[i] = basenet[i].get_value() + ndevs * numpy.sum([ (net[i] - basenet[i].get_value()) * (float(device.num_frames) / nframes) for net,dev in zip(hypnets,self.devices) ], axis = 0)
      for p, q in zip(self.network.train_params_vars, consnet):
        p.set_value(q)
        encoded.append(q)
      if len(hypnets) > 1:
        for device in self.devices:
          device.set_net_encoded_params(encoded)
    except Exception as e:
      print >> log.v3, "network synchronization failed: ", e.message
      if log.v4:
        sys.excepthook(*sys.exc_info())

    #pipe.copy_to_device(self.network)

  def evaluate(self, batchess, results, result_format, num_frames):
    """
    :param list[list[EngineBatch.Batch]] batchess: batches per device
    :param list[(float,params...)] results: result[i] is result for batch + i, result[i][0] is score
    :param list[str]|None result_format: describes what we have in a result list
    :type num_frames: int
    """
    assert results
    assert result_format  # train should always have the format
    assert num_frames > 0
    if self.share_batches:
      num_frames *= len(self.devices)
    results = [Device.make_result_dict(res, result_format) for res in results]
    cost = [res["cost"] for res in results]
    score = sum(cost)
    #if numpy.isinf(score) or numpy.isnan(score):
    #  for i, res in enumerate(results):
    #    if numpy.isinf(res["cost"]) or numpy.isnan(res["cost"]):
    #      raise ModelBrokenError("Model is broken, got %s score." % score, batchess[i])
    #  assert False  # Should not get here.
    if self.do_ctc_priors:
      for res in results:
        self.ctc_priors += res["ctc_priors"]
    self.score += score if not self.share_batches else score / len(self.devices)
    self.create_consensus(cost, num_frames)

  def finalize(self):
    assert self.num_frames > 0
    # Note: self.num_frames could be greater than self.data.get_num_timesteps() in case of chunking.
    self.score /= float(self.num_frames)
    if self.do_ctc_priors:
      self.ctc_priors /= float(self.num_frames)
    super(TrainTaskThread, self).finalize()


class EvalTaskThread(TaskThread):
    def __init__(self, network, devices, data, batches, **kwargs):
      super(EvalTaskThread, self).__init__('eval', network, devices, data=data, batches=batches, **kwargs)

    def initialize(self):
      self.score = 0
      self.error = 0
      for device in self.devices:
        device.set_net_params(self.network)

    def evaluate(self, batchess, results, result_format, num_frames):
      """
      :param list[list[EngineBatch.Batch]] batchess: batches per device
      :param list[list[numpy.ndarray]] results: results per device
      :type num_frames: int
      """
      assert results
      assert num_frames > 0
      score = sum([res[0] for res in results])
      error = sum([res[1] for res in results])
      self.score += score
      self.error += error

    def finalize(self):
      assert self.num_frames > 0
      self.score /= float(self.num_frames)
      if self.network.loss in ('ctc', 'ce_ctc'):
        assert self.num_frames == self.data.get_num_codesteps()  # Wrong otherwise. E.g. chunking.
        self.error /= float(self.data.num_running_chars)
      else:
        self.error /= float(self.num_frames)


class SprintCacheForwardTaskThread(TaskThread):
    def __init__(self, network, devices, data, batches, cache, merge={}):
      """
      :type network: Network.LayerNetwork
      :type devices: list[Device.Device]
      :type data: Dataset.Dataset
      :type batches: EngineBatch.BatchSetGenerator
      :type cache: SprintCache.FileArchive
      :type merge: dict
      :type start_batch: int
      """
      super(SprintCacheForwardTaskThread, self).__init__('extract', network, devices, data, batches)
      self.cache = cache
      self.merge = merge

    def initialize(self):
      self.toffset = 0

    def evaluate(self, batchess, results, result_format, num_frames):
      features = numpy.concatenate(results, axis = 1) #reduce(operator.add, device.result())
      if self.merge.keys():
        merged = numpy.zeros((len(features), len(self.merge.keys())), dtype = theano.config.floatX)
        for i in xrange(len(features)):
          for j, label in enumerate(self.merge.keys()):
            for k in self.merge[label]:
              merged[i, j] += numpy.exp(features[i, k])
          z = max(numpy.sum(merged[i]), 0.000001)
          merged[i] = numpy.log(merged[i] / z)
        features = merged
      # Currently we support just a single seq -> i.e. a single dev with a single batch.
      assert len(batchess) == 1
      assert len(batchess[0]) == 1
      batch = batchess[0][0]
      assert batch.get_num_seqs() == 1
      seq_idx = batch.start_seq
      print >> log.v5, "extracting", len(features[0]), "features over", len(features), "time steps for sequence", self.data.get_tag(seq_idx)
      times = zip(range(0, len(features)), range(1, len(features) + 1)) if not self.data.timestamps else self.data.timestamps[self.toffset : self.toffset + len(features)]
      #times = zip(range(0, len(features)), range(1, len(features) + 1))
      self.toffset += len(features)
      self.cache.addFeatureCache(self.data.get_tag(seq_idx), numpy.asarray(features), numpy.asarray(times))


class HDFForwardTaskThread(TaskThread):
    def __init__(self, network, devices, data, batches, cache, merge={}):
      super(HDFForwardTaskThread, self).__init__('extract', network, devices, data, batches, eval_batch_size=1)
      self.tags = []
      self.merge = merge
      self.cache = cache
      target = network.output['output'].attrs['target']
      cache.attrs['numSeqs'] = data.num_seqs
      cache.attrs['numTimesteps'] = data.get_num_timesteps()
      cache.attrs['inputPattSize'] = data.num_inputs
      cache.attrs['numDims'] = 1
      cache.attrs['numLabels'] = data.num_outputs[target]
      hdf5_strings(cache, 'targetlabels', data.labels[target])
      self.targets = { k: cache.create_dataset(k, (data.get_num_timesteps(),), dtype='i') for k in data.targets }
      self.seq_lengths = cache.create_dataset("seqLengths", (data.num_seqs,), dtype='i')
      self.seq_dims = cache.create_dataset("seqDims", (data.num_seqs, 1), dtype='i')
      self.times = []

    def initialize(self):
      self.toffset = 0

    def finalize(self):
      hdf5_strings(self.cache, 'seqTags', self.tags)
      if self.times:
        times = self.cache.create_dataset("times", (len(self.times), 2), dtype='f')
        times[...] = self.times

    def evaluate(self, batchess, results, result_format, num_frames):
      features = numpy.concatenate(results, axis=1)
      if not "inputs" in self.cache:
        self.inputs = self.cache.create_dataset("inputs", (self.cache.attrs['numTimesteps'], features.shape[2]), dtype='f', maxshape=(None, features.shape[2]))
      if self.merge.keys():
        merged = numpy.zeros((len(features), len(self.merge.keys())), dtype = theano.config.floatX)
        for i in xrange(len(features)):
          for j, label in enumerate(self.merge.keys()):
            for k in self.merge[label]:
              merged[i, j] += numpy.exp(features[i, k])
          z = max(numpy.sum(merged[i]), 0.000001)
          merged[i] = numpy.log(merged[i] / z)
        features = merged
      # Currently we support just a single seq -> i.e. a single dev with a single batch.
      assert len(batchess) == 1
      assert len(batchess[0]) == 1
      batch = batchess[0][0]
      assert batch.get_num_seqs() == 1
      seq_idx = batch.start_seq
      print >> log.v5, "extracting", features.shape[2], "features over", features.shape[1], "time steps for sequence", self.data.get_tag(seq_idx)
      self.seq_dims[seq_idx] = [features.shape[1]]
      self.seq_lengths[seq_idx] = features.shape[1]
      if self.inputs.shape[0] < self.toffset + features.shape[1]:
        self.inputs.resize(self.toffset + features.shape[1], axis = 0)
      self.inputs[self.toffset:self.toffset + features.shape[1]] = numpy.asarray(features)
      self.toffset += features.shape[1]
      self.tags.append(self.data.get_tag(seq_idx))
      self.times.extend(self.data.get_times(seq_idx))
