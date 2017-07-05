# ==========================================================================
# neural network Keras layers / blocks / loss functions needed for model
# ==========================================================================

import numpy as np

from consts import *
from nn_util import *
from keras import backend as K
from keras.models import *
from keras.layers import *
from keras.layers.core import *
from keras.layers.normalization import *
from keras.optimizers import *
from keras.regularizers import *
from keras.initializers import *
from keras.activations import softmax

# weight initialization used in all layers of network
W_INIT = 'he_normal'

# ---------------------------------------------------
# 1D "phase shift" upsampling layer, as discussed in [that one
# superresolution paper]
#
# Takes vector of size: B x S  x nC
# And returns vector:   B x nS x C
# ---------------------------------------------------
class PhaseShiftUp1D(Layer):
    def __init__(self, n, **kwargs):
        super(PhaseShiftUp1D, self).__init__(**kwargs)
        self.n = n
    
    def build(self, input_shape):
        # no trainable parameters
        self.trainable_weights = []
        super(PhaseShiftUp1D, self).build(input_shape)
        
    def call(self, x, mask=None):
        r = K.reshape(x, (-1, x.shape[1], x.shape[2] // self.n, self.n))
        r = K.permute_dimensions(r, (0, 1, 3, 2))
        r = K.reshape(r, (-1, x.shape[1] * self.n, x.shape[2] // self.n))
        return r
    
    def compute_output_shape(self, input_shape):
        return (input_shape[0], input_shape[1] * self.n, input_shape[2] // self.n)

    def get_config(self):
        config = {
            'n' : self.n,
        }
        base_config = super(PhaseShiftUp1D, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))

# ---------------------------------------------------
# Scalar quantization / dequantization layers
# ---------------------------------------------------

# both layers rely on the shared [QUANT_BINS] variable in consts.py

# quantization: takes in    [BATCH x WINDOW_SIZE x QUANT_CHANS]
#               and returns [BATCH x WINDOW_SIZE x QUANT_CHANS x NBINS]
# where the last dimension is a one-hot vector of bins
#
# [bins initialization is in consts.py]
class SoftmaxQuantization(Layer):
    def build(self, input_shape):
        self.SOFTMAX_TEMP = K.constant(500.0)
        self.trainable_weights = [QUANT_BINS]
        super(SoftmaxQuantization, self).build(input_shape)
        
    def call(self, x, mask=None):
        # x is an array: [BATCH x WINDOW_SIZE x QUANT_CHANS]
        # x_r becomes:   [BATCH x WINDOW_SIZE x QUANT_CHANS x 1]
        x_r = K.reshape(x, (-1, x.shape[1], x.shape[2], 1))
        
        # QUANT_BINS is an array: [QUANT_CHANS x NBINS]
        # q_r becomes:    [1 x 1 x QUANT_CHANS x NBINS]
        q_r = K.reshape(QUANT_BINS, (1, 1, x.shape[2], -1))
        
        # get L1 distance from each element to each of the bins
        # dist is: [BATCH x WINDOW_SIZE x QUANT_CHANS x NBINS]
        dist = K.abs(x_r - q_r)
        
        # turn into softmax probabilities, which we return
        enc = softmax(self.SOFTMAX_TEMP * -dist)
        
        # quantized version
        #quantized = K.one_hot(K.argmax(enc), NBINS)
        
        quant_on = enc# + K.stop_gradient(quantized - enc)
        quant_off = K.reshape(x, (-1, x.shape[1], x.shape[2], 1))
        quant_off = K.concatenate([quant_off,
                                   K.zeros_like(enc)[:, :, :, 1:]], axis = 3)
        
        return K.switch(QUANTIZATION_ON, quant_on, quant_off)
    
    def compute_output_shape(self, input_shape):
        return (input_shape[0], input_shape[1], input_shape[2], NBINS)

# dequantization: takes in    [BATCH x WINDOW_SIZE x QUANT_CHANS x NBINS]
#                 and returns [BATCH x WINDOW_SIZE x QUANT_CHANS]
class SoftmaxDequantization(Layer):
    def call(self, x, mask=None):
        dec = K.sum(x * QUANT_BINS, axis = -1)
        dec = K.reshape(dec, (-1, dec.shape[1], dec.shape[2]))

        quant_on = dec
        quant_off = K.reshape(x[:, :, :, :1], (-1, x.shape[1], x.shape[2]))
        return K.switch(QUANTIZATION_ON, quant_on, quant_off)
    
    def compute_output_shape(self, input_shape):
        return (input_shape[0], input_shape[1], input_shape[2])

# ---------------------------------------------------
# "Blocks" that make up all of our models
# ---------------------------------------------------

# activation used in all blocks
def activation(init = 0.3):
    # input is of form [nbatch x channel_size x num_channels],
    # so we share axis 1
    return PReLU(alpha_initializer = Constant(init),
                 shared_axes = [1])
    #return LeakyReLU(init)

# channel change block: takes input from however many channels
#                       it had before to [num_chans] channels,
#                       without applying any other operation
def channel_change_block(num_chans, filt_size):
    def f(inp):
        shortcut = Conv1D(num_chans, filt_size, padding = 'same',
                          kernel_initializer = W_INIT,
                          activation = 'linear')(inp)
        shortcut = activation(0.3)(shortcut)

        out = Conv1D(num_chans, filt_size, padding = 'same',
                     kernel_initializer = W_INIT,
                     activation = 'linear')(inp)
        out = activation(0.3)(out)

        out = Conv1D(num_chans, filt_size, padding = 'same',
                     kernel_initializer = W_INIT,
                     activation = 'linear')(out)
        out = activation(0.3)(out)

        out = Add()([out, shortcut])

        return out
    
    return f

# upsample block: takes input channels of length N and upsamples
#                 them to length 2N, using "phase shift" upsampling
def upsample_block(num_chans, filt_size):
    def f(inp):
        shortcut = Conv1D(num_chans * 2, filt_size, padding = 'same',
                          kernel_initializer = W_INIT,
                          activation = 'linear')(inp)
        shortcut = activation(0.3)(shortcut)
        shortcut = PhaseShiftUp1D(2)(shortcut)

        out = Conv1D(num_chans * 2, filt_size, padding = 'same',
                     kernel_initializer = W_INIT,
                     activation = 'linear')(inp)
        out = activation(0.3)(out)

        out = Conv1D(num_chans * 2, filt_size, padding = 'same',
                     kernel_initializer = W_INIT,
                     activation = 'linear')(out)
        out = activation(0.3)(out)
        out = PhaseShiftUp1D(2)(out)

        out = Add()([out, shortcut])

        return out
    
    return f

# downsample block: takes input channels of length N and downsamples
#                   them to length N/2, using strided convolution
def downsample_block(num_chans, filt_size):
    def f(inp):
        shortcut = Conv1D(num_chans, filt_size, padding = 'same',
                          kernel_initializer = W_INIT,
                          activation = 'linear',
                          strides = 2)(inp)
        shortcut = activation(0.3)(shortcut)

        out = Conv1D(num_chans, filt_size, padding = 'same',
                     kernel_initializer = W_INIT,
                     activation = 'linear',
                     strides = 2)(inp)
        out = activation(0.3)(out)

        out = Conv1D(num_chans, filt_size, padding = 'same',
                     kernel_initializer = W_INIT,
                     activation = 'linear')(out)
        out = activation(0.3)(out)

        out = Add()([out, shortcut])

        return out
    
    return f

# residual block
def residual_block(num_chans, filt_size, dilation = 1):
    def f(inp):
        shortcut = inp

        # conv1
        res = Conv1D(num_chans, filt_size, padding = 'same',
                     kernel_initializer = W_INIT,
                     activation = 'linear',
                     dilation_rate = dilation)(inp)
        res = activation(0.3)(res)

        # conv2
        res = Conv1D(num_chans, filt_size, padding = 'same',
                     kernel_initializer = W_INIT,
                     activation = 'linear',
                     dilation_rate = dilation)(res)
        res = activation(0.3)(res)

        return Add()([res, shortcut])
    
    return f


# ---------------------------------------------------
# Loss functions
# ---------------------------------------------------

# entropy weight variable
tau = K.variable(0.0001, name = "entropy_weight")

def code_entropy(placeholder, code):
    # [BATCH_SIZE x QUANT_CHAN x NBINS]
    #     => [QUANT_CHANS x NBINS]
    # probability distribution over symbols for each channel
    all_onehots = K.reshape(code, (-1, QUANT_CHANS, NBINS))
    onehot_hist = K.sum(all_onehots, axis = 0)
    onehot_hist /= K.sum(onehot_hist, axis = 1, keepdims = True)

    # entropy for each channel
    channel_entropy = -K.sum(onehot_hist * K.log(onehot_hist + K.epsilon()) / K.log(2.0),
                             axis = 1)

    # total entropy
    entropy = K.sum(channel_entropy)
    
    loss = tau * entropy
    return K.switch(QUANTIZATION_ON, loss, K.zeros_like(loss))

def code_sparsity(placeholder, code):
    # [BATCH_SIZE x CHANNEL_SIZE x QUANT_CHANS x NBINS]
    #     => [BATCH_SIZE x CHANNEL_SIZE x QUANT_CHANS]
    square_sum = K.sum(K.sqrt(code + K.epsilon()), axis = -1) - 1.0
    
    # take sum over channels, mean over sum
    sparsity = K.mean(K.sum(square_sum, axis = -1), axis = -1)
    return K.switch(QUANTIZATION_ON, sparsity, K.zeros_like(sparsity))



















