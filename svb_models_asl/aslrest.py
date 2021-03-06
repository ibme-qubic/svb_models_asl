"""
Inference forward models for ASL data
"""
try:
    import tensorflow.compat.v1 as tf
except ImportError:
    import tensorflow as tf

import numpy as np

from svb.model import Model, ModelOption
from svb.utils import ValueList
from svb.parameter import get_parameter

from svb_models_asl import __version__

class AslRestModel(Model):
    """
    ASL resting state model

    FIXME integrate with oxasl AslImage class?
    """

    OPTIONS = [

        # ASL parameters 
        ModelOption("tau", "Bolus duration", units="s", clargs=("--tau", "--bolus"), type=float, default=1.8),
        ModelOption("casl", "Data is CASL/pCASL", type=bool, default=False),
        ModelOption("tis", "Inversion times", units="s", type=ValueList(float)),
        ModelOption("plds", "Post-labelling delays (for CASL instead of TIs)", units="s", type=ValueList(float)),
        ModelOption("repeats", "Number of repeats - single value or one per TI/PLD", units="s", type=ValueList(int), default=[1]),
        ModelOption("slicedt", "Increase in TI/PLD per slice", units="s", type=float, default=0),

        # GM tissue properties 
        ModelOption("t1", "Tissue T1 value", units="s", type=float, default=1.3),
        ModelOption("att", "Bolus arrival time", units="s", clargs=("--bat",), type=float, default=1.3),
        ModelOption("attsd", "Bolus arrival time prior std.dev.", units="s", clargs=("--batsd",), type=float, default=None),
        ModelOption("fcalib", "Perfusion value to use in estimation of effective T1", type=float, default=0.01),
        ModelOption("pc", "Blood/tissue partition coefficient. If only inferring on one tissue, default is 0.9; if inferring on both GM/WM default is 0.98/0.8 respectively. See --pcwm", type=float, default=None),

        # WM tissue properties 
        ModelOption("incwm", "Include WM parameters", default=False),
        ModelOption("fwm", "WM perfusion", type=float, default=0),
        ModelOption("attwm", "WM arterial transit time", clargs="--batwm", type=float, default=1.6),
        ModelOption("t1wm", "WM T1 value", units="s", type=float, default=1.1),
        ModelOption("pcwm", "WM parition coefficient. See --pc", type=float, default=0.8),
        ModelOption("fcalibwm", "WM perfusion value to use in estimation of effective T1", type=float, default=0.003),

        # Blood / arterial properties 
        ModelOption("t1b", "Blood T1 value", units="s", type=float, default=1.65),
        ModelOption("artt", "Arterial bolus arrival time", units="s", clargs=("--batart",), type=float, default=None),
        ModelOption("arttsd", "Arterial bolus arrival time prior std.dev.", units="s", clargs=("--batartsd",), type=float, default=None),

        # Inference options 
        ModelOption("artonly", "Only infer arterial component not tissue", type=bool),
        ModelOption("inferart", "Infer arterial component", type=bool),
        ModelOption("infert1", "Infer T1 value", type=bool),
        ModelOption("att_init", "Initialization method for ATT (max=max signal - bolus duration)", default=""),
        ModelOption("pvcorr", "Perform PVEc (shortcut for incwm, inferwm)", default=False),
        ModelOption("inferwm", "Infer WM parameters", default=False),

        # PVE options 
        ModelOption("pvgm", "GM partial volume", type=float, default=1.0),
        ModelOption("pvwm", "WM partial volume", type=float, default=0.0),

    ]

    def __init__(self, data_model, **options):
        Model.__init__(self, data_model, **options)
        if self.plds is not None:
            self.tis = [self.tau + pld for pld in self.plds]

        if self.tis is None and self.plds is None:
            raise ValueError("Either TIs or PLDs must be given")

        if self.attsd is None:
            self.attsd = 1.0 if len(self.tis) > 1 else 0.1
        if self.artt is None:
            self.artt = self.att - 0.3
        if self.arttsd is None:
            self.arttsd = self.attsd

        # Repeats are supposed to be a list but can be a single number
        if isinstance(self.repeats, int):
            self.repeats = [self.repeats]

        # For now we only support fixed repeats
        if len(self.repeats) == 1:
            # FIXME variable repeats
            self.repeats = self.repeats[0]
        elif len(self.repeats) > 1 and \
            any([ r != self.repeats[0] for r in self.repeats ]):
            raise NotImplementedError("Variable repeats for TIs/PLDs")

        if self.pvcorr: 
            self.incwm = True
            self.inferwm = True 

            # Ensure PVs match data size, whether provided as an array, 
            # single scalar, or path to file. 
            if isinstance(self.pvgm, (float,int)):
                gm = np.array(self.pvgm) 
                wm = np.array(self.pvwm)
            else:
                gm = data_model._get_data(self.pvgm)[1].flatten()
                wm = data_model._get_data(self.pvwm)[1].flatten()

            if gm.size > self.data_model.n_nodes: 
                gm = gm[data_model.mask_flattened]
                wm = wm[data_model.mask_flattened]

            ones = np.ones(data_model.n_nodes)
            self.pvgm = (ones * gm).astype(np.float32)
            self.pvwm = (ones * wm).astype(np.float32)

        # If no pc provided, default depends on inclusion of WM or not. 
        if self.pc is None: 
            if self.incwm: 
                self.pc = 0.98
            else: 
                self.pc = 0.9

        if self.incwm:
            self.pc = 0.98

        if self.artonly:
            self.inferart = True

        if not self.artonly:
            self.params = [
                get_parameter("ftiss", dist="Normal", 
                            mean=1.5, prior_var=1e6, post_var=1.5, 
                            post_init=self._init_flow,
                            **options),
                get_parameter("delttiss", dist="Normal", 
                            mean=self.att, var=self.attsd**2,
                            post_init=self._init_delt,
                            **options)
            ]

            if self.inferwm: 
                self.params += [
                    get_parameter("fwm", dist="Normal", 
                            mean=0.5, prior_var=1e6, post_var=1.5, 
                            post_init=self._init_flow,
                            **options),
                    get_parameter("deltwm", dist="Normal", 
                            mean=self.attwm, var=self.attsd**2,
                            post_init=self._init_delt,
                            **options)
                ]

        if self.infert1:
            self.params.append(
                get_parameter("t1", mean=self.t1, var=0.01, **options)
            )

            if self.inferwm:
                self.params.append(
                    get_parameter("t1wm", mean=self.t1wm, var=0.01, **options)
                )

        if self.inferart:
            self.leadscale = 0.01
            self.params.append(
                get_parameter("fblood", dist="Normal",
                              mean=0.0, prior_var=1e6, post_var=1.5,
                              post_init=self._init_fblood,
                              prior_type="A",
                              **options)
            )
            self.params.append(
                get_parameter("deltblood", dist="Normal", 
                              mean=self.artt, var=self.arttsd**2,
                              post_init=self._init_delt,
                              **options)
            )

    def evaluate(self, params, tpts):
        """
        Basic PASL/pCASL kinetic model

        :param t: Time values tensor of shape [W, 1, N] or [1, 1, N]
        :param params Sequence of parameter values arrays, one for each parameter.
                      Each array is [W, S, 1] tensor where W is the number of nodes and
                      S the number of samples. This
                      may be supplied as a [P, W, S, 1] tensor where P is the number of
                      parameters.

        :return: [W, S, N] tensor containing model output at the specified time values
                 and for each time value using the specified parameter values
        """
        # Extract parameter tensors
        t = self.log_tf(tpts, name="tpts", shape=True)
        param_idx = 0
        if not self.artonly:
            ftiss = self.log_tf(params[param_idx], name="ftiss", shape=True)
            param_idx += 1
            delt = self.log_tf(params[param_idx], name="delt", shape=True)
            param_idx += 1
            if self.inferwm:
                fwm = self.log_tf(params[param_idx], name="fwm", shape=True)
                param_idx += 1
                deltwm = self.log_tf(params[param_idx], name="deltwm", shape=True)
                param_idx += 1   
            else: 
                fwm = self.fwm 
                deltwm = self.attwm              
    
        if self.infert1:
            t1 = self.log_tf(params[param_idx], name="t1", shape=True)
            param_idx += 1
            if self.inferwm: 
                t1wm = self.log_tf(params[param_idx], name="t1wm", shape=True)
                param_idx += 1 

        else:
            t1 = self.t1
            t1wm = self.t1wm

        if self.inferart:
            fblood = self.log_tf(params[param_idx], name="fblood", shape=True, force=False)
            deltblood = self.log_tf(params[param_idx+1], name="deltblood", shape=True, force=False)
            param_idx += 2

        # Extra parameters may be required by subclasses, e.g. dispersion parameters
        # FIXME: will need to separate out WM extra params and GM extra params... 
        extra_params = params[param_idx:]

        # PV estimates are rank-1, but params may be rank-2 or 3, so expand dims
        # to match these before doing multiplication.
        if not self.artonly:
            gm = np.asarray(self.pvgm)
            while gm.ndim < len(ftiss.shape): gm = gm[...,None]
            gmsignal = self.log_tf(
                gm * self.tissue_signal(t, ftiss, delt, t1, self.fcalib, extra_params), 
                name="tiss_signal")
            if self.incwm: 
                wm = np.asarray(self.pvwm)
                while wm.ndim < len(fwm.shape): wm = wm[...,None]
                wmsignal = self.log_tf(
                    wm * self.tissue_signal(t, fwm, deltwm, t1wm, self.fcalibwm, extra_params),
                    name="wm_tiss_signal")
                signal = wmsignal + gmsignal 
            else: 
                signal = gmsignal 

        else:
            signal = tf.zeros(tf.shape(t), dtype=tf.float32)

        if self.inferart:
            signal += self.log_tf(self.art_signal(t, fblood, deltblood, extra_params), name="art_signal")

        return self.log_tf(signal, name="asl_signal")

    def tissue_signal(self, t, ftiss, delt, t1, fcalib, extra_params):
        """
        PASL/pCASL kinetic model for tissue
        """

        if (extra_params != []) and (extra_params.shape[0] > 0): 
            raise NotImplementedError("Extra tissue parameters not set up yet")

        # Boolean masks indicating which voxel-timepoints are during the
        # bolus arrival and which are after
        post_bolus = self.log_tf(tf.greater(t, tf.add(self.tau, delt), name="post_bolus"), shape=True)
        during_bolus = tf.logical_and(tf.greater(t, delt), tf.logical_not(post_bolus))

        # Rate constants
        t1_app = 1 / (1 / t1 + fcalib / self.pc)

        # Calculate signal
        if self.casl:
            # CASL kinetic model
            factor = 2 * t1_app * tf.exp(-delt / self.t1b)
            during_bolus_signal = factor * (1 - tf.exp(-(t - delt) / t1_app))
            post_bolus_signal = factor * tf.exp(-(t - self.tau - delt) / t1_app) * (1 - tf.exp(-self.tau / t1_app))
        else:
            # PASL kinetic model
            r = 1 / t1_app - 1 / self.t1b
            f = 2 * tf.exp(-t / t1_app)
            factor = f / r
            during_bolus_signal = factor * ((tf.exp(r * t) - tf.exp(r * delt)))
            post_bolus_signal = factor * ((tf.exp(r * (delt + self.tau)) - tf.exp(r * delt)))

        post_bolus_signal = self.log_tf(post_bolus_signal, name="post_bolus_signal", shape=True)
        during_bolus_signal = self.log_tf(during_bolus_signal, name="during_bolus_signal", shape=True)

        # Build the signal from the during and post bolus components leaving as zero
        # where neither applies (i.e. pre bolus)
        signal = tf.zeros(tf.shape(during_bolus_signal))
        signal = tf.where(during_bolus, during_bolus_signal, signal)
        signal = tf.where(post_bolus, post_bolus_signal, signal)

        return ftiss*signal

    def art_signal(self, t, fblood, deltblood, extra_params):
        """
        PASL/pCASL Kinetic model for arterial curve
        
        To avoid problems with the discontinuous gradient at ti=deltblood
        and ti=deltblood+taub, we smooth the transition at these points
        using a Gaussian convolved step function. The sigma value could
        be exposed as a parameter (small value = less smoothing). This is
        similar to the effect of Gaussian dispersion, but can be computed
        without numerical integration
        """
        if self.casl:
            kcblood = 2 * tf.exp(-deltblood / self.t1b)
        else:
            kcblood = 2 * tf.exp(-t / self.t1b)

        # Boolean masks indicating which voxel-timepoints are in the leadin phase
        # and which in the leadout
        leadout = tf.greater(t, tf.add(deltblood, self.tau/2))
        leadin = self.log_tf(tf.logical_not(leadout), name="leadin1", shape=True)

        # If deltblood is smaller than the lead in scale, we could 'lose' some
        # of the bolus, so reduce degree of lead in as deltblood -> 0. We
        # don't really need it in this case anyway since there will be no
        # gradient discontinuity
        leadscale = tf.minimum(deltblood, self.leadscale)
        leadin = self.log_tf(tf.logical_and(leadin, tf.greater(leadscale, 0)), shape=True)

        # Calculate lead-in and lead-out signals
        leadin_signal = self.log_tf(kcblood * 0.5 * (1 + tf.math.erf((t - deltblood) / leadscale)), name="leadin_signal", shape=True)
        leadout_signal = kcblood * 0.5 * (1 + tf.math.erf(-(t - deltblood - self.tau) / self.leadscale))

        # Form final signal from combination of lead in and lead out signals
        signal = tf.zeros(tf.shape(leadin_signal))
        signal = tf.where(leadin, leadin_signal, signal)
        signal = tf.where(leadout, leadout_signal, signal)

        return fblood*signal

    def tpts(self):
        if self.data_model.n_tpts != len(self.tis) * self.repeats:
            raise ValueError("ASL model configured with %i time points, but data has %i" % (len(self.tis)*self.repeats, self.data_model.n_tpts))

        # FIXME assuming grouped by TIs/PLDs
        # Generate timings volume using the slicedt value
        t = np.zeros(list(self.data_model.shape) + [self.data_model.n_tpts], dtype=np.float32)
        for z in range(self.data_model.shape[2]):
            t[:, :, z, :] = np.array(sum([[ti + z*self.slicedt] * self.repeats for ti in self.tis], []))

        # Unmasked voxel timings
        t = t[self.data_model.mask_vol > 0]

        # Time points derived from volumetric data need to be transformed
        # into node space.
        if not self.data_model.is_volumetric:
            t = t.reshape(-1, 1, self.data_model.n_tpts)
            with tf.Session() as sess:
                t = self.data_model.voxels_to_nodes_ts(t, edge_scale=False)
                t = sess.run(tf.identity(t))
            # HACK to fix the fact that conversion tensors will
            # be cached in the wrong graph
            self.data_model.uncache_tensors()

        return t.reshape(-1, self.data_model.n_tpts)

    def __str__(self):
        return "ASL resting state model: %s" % __version__

    def _init_flow(self, _param, _t, data):
        """
        Initial value for the flow parameter
        """
        # return f, None 
        if not self.pvcorr:
            f = tf.math.maximum(tf.reduce_max(data, axis=1), 0.1)
            return f, None
        else:
            # Do a quick edge correction to up-scale signal in edge voxels 
            # Guard against small number division 
            pvsum = self.pvgm + self.pvwm
            edge_data = data / np.maximum(pvsum, 0.3)[:,None]

            # Intialisation for PVEc: assume a CBF ratio of 3:1, 
            # let g = GM PV, w = WM PV = (1 - g), f = raw CBF, 
            # x = WM CBF. Then, wx + 3gx = f => x = 3f / (1 + 2g)
            f = tf.math.maximum(tf.reduce_max(edge_data, axis=1), 0.1)
            fwm = f / (1 + 2*self.pvgm)
            if _param.name == 'fwm':
                return fwm, None
            else: 
                return 3 * fwm, None 


    def _init_fblood(self, _param, _t, data):
        """
        Initial value for the fblood parameter
        """
        return tf.math.maximum(tf.reduce_max(data, axis=1), 0.1), None


    def _init_delt(self, _param, t, data):
        """
        Initial value for the delttiss parameter
        """
        if self.att_init == "max":
            t = self.log_tf(t, name="t", force=True, shape=True)
            data = self.log_tf(data, name="data", force=True, shape=True)
            max_idx = self.log_tf(tf.expand_dims(tf.math.argmax(data, axis=1), -1), shape=True, force=True, name="max_idx")
            time_max = self.log_tf(tf.squeeze(tf.gather(t, max_idx, axis=1, batch_dims=1), axis=-1), shape=True, force=True, name="time_max")

            if _param.name == 'fwm': 
                return time_max + 0.3 - self.tau, None
            else: 
                return time_max - self.tau, None
        else:
            return None, None
