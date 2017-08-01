####################################################################################################
#
# Project:  Embedded Learning Library (ELL)
# File:     cntk_layers.py (importers)
# Authors:  Byron Changuion, Lisa Ong
#
# Requires: Python 3.x, cntk-2.0-cp35
#
####################################################################################################

"""Imports CNTK layers to ELL equivalents"""

import ELL
from cntk.initializer import glorot_uniform, he_normal
from cntk.layers import Convolution, MaxPooling, AveragePooling, Dropout, BatchNormalization, Dense
import cntk.layers.blocks
from cntk.layers.typing import *
from cntk.ops import *
from cntk import load_model
from cntk.logging.graph import *

import lib.cntk_converters as converters
import lib.cntk_utilities as utilities


class BaseLayer:
    """Base class with common layer processing functionality"""

    def __init__(self, layer):
        self.layer = layer
        self.layer.ell_inputPaddingParameters = self.get_input_padding_parameters()

        if not hasattr(self, 'input_shape'):
            if (len(self.layer.arguments) > 0 and len(self.layer.arguments[0].shape) > 0):
                self.input_shape = self.layer.arguments[0].shape
        # else, assume derived classes have already initialized the input shape

        if hasattr(self, 'input_shape'):
            self.layer.ell_inputShape = utilities.get_adjusted_shape(
                self.input_shape, self.layer.ell_inputPaddingParameters)
        else:
            raise RuntimeError(
                "Could not initialize input_shape")  # coding error

    def __repr__(self):
        """Prints summary info about this layer.
           Derived classes may override this.
        """
        return " ".join((self.op_name, ": ", utilities.ell_shape_to_string(self.layer.ell_inputShape), " -> ",
                         utilities.ell_shape_to_string(
            self.layer.ell_outputShape),
            "| input padding", str(
                self.layer.ell_inputPaddingParameters.paddingSize),
            " output padding", str(self.layer.ell_outputPaddingParameters.paddingSize)))

    def get_input_padding_parameters(self):
        """Returns the default ELL.PaddingParameters for a layer's input.
           Derived classes may override this.
        """

        return ELL.PaddingParameters(ELL.PaddingScheme.zeros, 0)

    def set_output_characteristics(self, nextLayer):
        """Sets the output characteristics based on the next layer"""

        if (nextLayer is not None):
            self.layer.ell_outputPaddingParameters = nextLayer.layer.ell_inputPaddingParameters
            self.layer.ell_outputShape = utilities.get_adjusted_shape(
                self.layer.output.shape, self.layer.ell_outputPaddingParameters)
            self.layer.ell_outputShapeMinusPadding = utilities.get_shape(
                self.layer.output.shape)
        else:
            # last layer
            self.layer.ell_outputPaddingParameters = ELL.NoPadding()
            self.layer.ell_outputShape = utilities.get_adjusted_shape(
                self.layer.output.shape, ELL.NoPadding())
            self.layer.ell_outputShapeMinusPadding = self.layer.ell_outputShape

    def process(self, ellLayers):
        """Appends the ELL equivalent of the current layer to ellLayers.
           Derived classes must override this.
        """

        raise NotImplementedError(
            "Error: subclasses must override this method")


class DenseLayer(BaseLayer):
    """Logic for converting a CNTK Dense layer to ELL"""

    def __init__(self, layer):
        if not layer.is_block:
            raise ValueError("Dense node is not a block node")

        self.op_name = 'Dense'
        super().__init__(layer)

    def process(self, ellLayers):
        """Appends the ELL equivalent of the current layer to ellLayers."""

        # Note that a single CNTK Dense function block is equivalent to the following 3 ELL layers:
        # - FullyConnectedLayer
        # - BiasLayer
        # - ActivationLayer. This layer is sometimes missing, depending on activation type.
        #
        # Therefore, make sure the output padding characteristics of the last layer reflect the next layer's
        # padding requirements.

        weightsParameter = utilities.find_parameter_by_name(
            self.layer.parameters, 'W', 0)
        biasParameter = utilities.find_parameter_by_name(
            self.layer.parameters, 'b', 1)
        weightsTensor = converters.get_float_tensor_from_cntk_dense_weight_parameter(
            weightsParameter)
        biasVector = converters.get_float_vector_from_cntk_trainable_parameter(
            biasParameter)

        # Create the ELL.LayerParameters for the various ELL layers
        firstLayerParameters = ELL.LayerParameters(
            self.layer.ell_inputShape, self.layer.ell_inputPaddingParameters, self.layer.ell_outputShapeMinusPadding, ELL.NoPadding())
        middleLayerParameters = ELL.LayerParameters(self.layer.ell_outputShapeMinusPadding, ELL.NoPadding(
        ), self.layer.ell_outputShapeMinusPadding, ELL.NoPadding())
        lastLayerParameters = ELL.LayerParameters(self.layer.ell_outputShapeMinusPadding, ELL.NoPadding(
        ), self.layer.ell_outputShape, self.layer.ell_outputPaddingParameters)

        layerParameters = firstLayerParameters

        internalNodes = utilities.get_model_layers(self.layer.block_root)
        activationType = utilities.get_activation_type(internalNodes)

        # Create the ELL fully connected layer
        ellLayers.append(ELL.FloatFullyConnectedLayer(
            layerParameters, weightsTensor))

        # Create the ELL bias layer
        if (utilities.is_softmax_activation(internalNodes) or activationType != None):
            layerParameters = middleLayerParameters
        else:
            layerParameters = lastLayerParameters
        ellLayers.append(ELL.FloatBiasLayer(layerParameters, biasVector))

        # Create the ELL activation layer
        if (utilities.is_softmax_activation(internalNodes) or activationType != None):
            layerParameters = lastLayerParameters

            # Special case: if this is softmax activation, create an ELL Softmax layer.
            # Else, insert an ELL ActivationLayer
            if(utilities.is_softmax_activation(internalNodes)):
                ellLayers.append(ELL.FloatSoftmaxLayer(layerParameters))
            else:
                if (activationType != None):
                    ellLayers.append(ELL.FloatActivationLayer(
                        layerParameters, activationType))


class BinaryConvolutionLayer(BaseLayer):
    """Logic for converting a CNTK Binary Convolution layer to ELL"""

    def __init__(self, layer):
        if layer.is_block:
            raise ValueError(
                "Error: Binary Convolution layer node is in block node")

        self.op_name = 'BinaryConvolution'

        # Convolution function (ASSUME part of a Binary Convolution layer)
        # - Weights is 4-dimensional (filters, channels, rows, columns)
        # - Input is 3-dimensional (channels, rows, columns)
        # - Bias is a separate layer and not processed by this class
        # - Activation is a separate layer and not processed by this class
        if len(layer.inputs[0].shape) == 3:
            self.input_parameter = layer.inputs[0]
            weights_input = layer.inputs[1]
        else:
            self.input_parameter = layer.inputs[1]
            weights_input = layer.inputs[0]

        self.weights_parameter = utilities.find_parameter_by_name(
            weights_input.owner.parameters, 'filter')
        self.attributes = layer.attributes

        # Determine the binarization method used for weights based on the
        # name attributes of the UserFunctions defined in the custom_functions.py
        # used during training.
        # Until we can find a better heuristic, assume that the custom function names
        # don't change across models.
        function_name = weights_input.owner.name
        if function_name == 'Sign':
            self.convolution_method = ELL.BinaryConvolutionMethod.bitwise
            self.weights_scale = ELL.BinaryWeightsScale.none
        else:
            raise ValueError(
                "Error: unrecognized binarization function: " + function_name)

        self.input_shape = self.input_parameter.shape

        super().__init__(layer)

    def get_input_padding_parameters(self):
        """Returns the ELL.PaddingParameters for a layer's input."""

        paddingScheme = ELL.PaddingScheme.zeros
        padding = 0
        receptiveField = self.weights_parameter.shape[2]

        if ('autoPadding' in self.attributes):
            if (self.attributes['autoPadding'][1] == True):
                padding = int((receptiveField - 1) / 2)
            else:
                padding = self.attributes['upperPad'][0]
        else:
            padding = self.attributes['upperPad'][0]

        return ELL.PaddingParameters(paddingScheme, padding)

    def process(self, ellLayers):
        """Helper to convert a binary convolutional layer to the ELL equivalent."""

        # A CNTK Binary Convolutional layer is a single function.
        # Bias and Activation are separate layers (processed outside of this class).
        weightsTensor = converters.get_float_tensor_from_cntk_convolutional_weight_parameter(
            self.weights_parameter)

        layerParameters = ELL.LayerParameters(
            self.layer.ell_inputShape, self.layer.ell_inputPaddingParameters, self.layer.ell_outputShape,
            self.layer.ell_outputPaddingParameters)

        # Fill in the convolutional parameters
        weightsShape = self.weights_parameter.shape
        receptiveField = weightsShape[2]
        stride = self.attributes['strides'][2]

        convolutionalParameters = ELL.BinaryConvolutionalParameters(
            receptiveField, stride, self.convolution_method, self.weights_scale)

        ellLayers.append(ELL.FloatBinaryConvolutionalLayer(
            layerParameters, convolutionalParameters, weightsTensor))


class ConvolutionLayer(BaseLayer):
    """Logic for converting a CNTK Convolution layer to ELL"""

    def __init__(self, layer):
        if not layer.is_block:
            raise ValueError(
                "Error: Convolution layer node is not in block node")

        self.op_name = 'Convolution'

        # initialize weights and input characteristics
        self.input_parameter = layer.arguments[0]
        self.weights_parameter = utilities.find_parameter_by_name(
            layer.parameters, 'W', 0)
        self.bias_parameter = utilities.find_parameter_by_name(
            layer.parameters, 'b', 1)

        # Get the hyper-parameters for the convolution.
        # They are on the convolution node inside this block.
        convolution_nodes = depth_first_search(
            layer.block_root, lambda x: utilities.op_name_equals(x, 'Convolution'))

        self.attributes = convolution_nodes[0].attributes
        self.convolution_method = 0
        self.input_shape = self.input_parameter.shape

        super().__init__(layer)

    def __repr__(self):
        """Prints summary info about this layer."""

        label = self.op_name
        nodes = utilities.get_model_layers(self.layer.block_root)
        if utilities.is_softmax_activation(nodes):
            label += "(softmax)"
        else:
            activation_type = utilities.get_activation_type(nodes)
            if activation_type is not None:
                label += "(" + utilities.ell_activation_type_to_string(activation_type) + ")"

        return " ".join((label, ": ", utilities.ell_shape_to_string(self.layer.ell_inputShape), " -> ",
                         utilities.ell_shape_to_string(
            self.layer.ell_outputShape),
            "| input padding", str(
                self.layer.ell_inputPaddingParameters.paddingSize),
            " output padding", str(self.layer.ell_outputPaddingParameters.paddingSize)))

    def get_input_padding_parameters(self):
        """Returns the ELL.PaddingParameters for a layer's input."""

        paddingScheme = ELL.PaddingScheme.zeros
        padding = 0
        receptiveField = self.weights_parameter.shape[2]

        if ('autoPadding' in self.attributes):
            if (self.attributes['autoPadding'][1] == True):
                padding = int((receptiveField - 1) / 2)
            else:
                padding = self.attributes['upperPad'][0]
        else:
            padding = self.attributes['upperPad'][0]

        return ELL.PaddingParameters(paddingScheme, padding)

    def process(self, ellLayers):
        """Helper to convert a convolutional layer to the ELL equivalent."""

        # Note that a single CNTK Convolutional function block is equivalent to the following 3 ELL layers:
        # - ConvolutionalLayer
        # - BiasLayer
        # - ActivationLayer. This layer is sometimes missing, depending on activation type.
        #
        # Therefore, make sure the output padding characteristics of the last layer reflect the next layer's
        # padding requirements.

        weightsTensor = converters.get_float_tensor_from_cntk_convolutional_weight_parameter(
            self.weights_parameter)
        biasVector = converters.get_float_vector_from_cntk_trainable_parameter(
            self.bias_parameter)

        # Create the ELL.LayerParameters for the various ELL layers
        firstLayerParameters = ELL.LayerParameters(
            self.layer.ell_inputShape, self.layer.ell_inputPaddingParameters, self.layer.ell_outputShapeMinusPadding, ELL.NoPadding())
        middleLayerParameters = ELL.LayerParameters(self.layer.ell_outputShapeMinusPadding, ELL.NoPadding(
        ), self.layer.ell_outputShapeMinusPadding, ELL.NoPadding())
        lastLayerParameters = ELL.LayerParameters(self.layer.ell_outputShapeMinusPadding, ELL.NoPadding(
        ), self.layer.ell_outputShape, self.layer.ell_outputPaddingParameters)

        layerParameters = firstLayerParameters

        # Fill in the convolutional parameters
        weightsShape = self.weights_parameter.shape
        receptiveField = weightsShape[2]
        stride = self.attributes['strides'][2]

        filterBatchSize = layerParameters.outputShape.channels

        internalNodes = utilities.get_model_layers(self.layer.block_root)
        activationType = utilities.get_activation_type(internalNodes)

        convolutionalParameters = ELL.ConvolutionalParameters(
            receptiveField, stride, self.convolution_method, filterBatchSize)

        # Create the ELL convolutional layer
        ellLayers.append(ELL.FloatConvolutionalLayer(
            layerParameters, convolutionalParameters, weightsTensor))

        # Create the ELL bias layer
        isSoftmaxActivation = utilities.is_softmax_activation(internalNodes)
        hasActivation = isSoftmaxActivation or activationType != None
        if (hasActivation):
            layerParameters = middleLayerParameters
        else:
            layerParameters = lastLayerParameters
        ellLayers.append(ELL.FloatBiasLayer(layerParameters, biasVector))

        # Create the ELL activation layer
        if (hasActivation):
            layerParameters = lastLayerParameters

            # Special case: if this is softmax activation, create an ELL Softmax layer.
            # Else, insert an ELL ActivationLayer
            if (isSoftmaxActivation):
                ellLayers.append(ELL.FloatSoftmaxLayer(layerParameters))
            else:
                ellLayers.append(ELL.FloatActivationLayer(
                    layerParameters, activationType))


class LinearLayer(BaseLayer):
    """Logic for converting a CNTK Linear layer to ELL"""

    def __init__(self, layer):
        self.op_name = 'linear'
        super().__init__(layer)

    def process(self, ellLayers):
        """Appends the ELL representation of the current layer to ellLayers."""

        # Note that a single CNTK Linear function block is equivalent to the following 3 ELL layers:
        # - FullyConnectedLayer
        # - BiasLayer
        # - ActivationLayer. This layer is sometimes missing, depending on activation type.
        #
        # Therefore, make sure the output padding characteristics of the last layer reflect the next layer's
        # padding requirements.

        weightsParameter = utilities.find_parameter_by_name(
            self.layer.parameters, 'W', 0)
        biasParameter = utilities.find_parameter_by_name(
            self.layer.parameters, 'b', 1)
        weightsTensor = converters.get_float_tensor_from_cntk_dense_weight_parameter(
            weightsParameter)
        biasVector = converters.get_float_vector_from_cntk_trainable_parameter(
            biasParameter)

        # Create the ELL.LayerParameters for the various ELL layers
        firstLayerParameters = ELL.LayerParameters(
            self.layer.ell_inputShape, self.layer.ell_inputPaddingParameters, self.layer.ell_outputShapeMinusPadding, ELL.NoPadding())
        middleLayerParameters = ELL.LayerParameters(self.layer.ell_outputShapeMinusPadding, ELL.NoPadding(
        ), self.layer.ell_outputShapeMinusPadding, ELL.NoPadding())
        lastLayerParameters = ELL.LayerParameters(self.layer.ell_outputShapeMinusPadding, ELL.NoPadding(
        ), self.layer.ell_outputShape, self.layer.ell_outputPaddingParameters)

        layerParameters = firstLayerParameters

        internalNodes = utilities.get_model_layers(self.layer.block_root)
        activationType = utilities.get_activation_type(internalNodes)

        # Create the ELL fully connected layer
        ellLayers.append(ELL.FloatFullyConnectedLayer(
            layerParameters, weightsTensor))

        # Create the ELL bias layer
        isSoftmaxActivation = utilities.is_softmax_activation(internalNodes)
        hasActivation = isSoftmaxActivation or activationType != None
        if (hasActivation):
            layerParameters = middleLayerParameters
        else:
            layerParameters = lastLayerParameters
        ellLayers.append(ELL.FloatBiasLayer(layerParameters, biasVector))

        # Create the ELL activation layer
        if (hasActivation):
            layerParameters = lastLayerParameters

            # Special case: if this is softmax activation, create an ELL Softmax layer.
            # Else, insert an ELL ActivationLayer
            if (isSoftmaxActivation):
                ellLayers.append(ELL.FloatSoftmaxLayer(layerParameters))
            else:
                ellLayers.append(ELL.FloatActivationLayer(
                    layerParameters, activationType))


class ElementTimesLayer(BaseLayer):
    """Logic for converting a CNTK ElementTimes layer to ELL"""

    def __init__(self, layer):
        if (len(layer.parameters) != 1 and len(layer.constants) != 1):
            raise ValueError(
                "Skipping ElementTimes layer due to dimensions of Constants and Parameters")

        self.op_name = 'ElementTimes'
        if (len(layer.constants) > 0):
            self.scale = layer.constants[0]
        elif (len(layer.parameters) > 0):
            self.scale = layer.parameters[0]

        super().__init__(layer)

    def process(self, ellLayers):
        """Appends the ELL representation of the current layer to ellLayers."""

        # Create the ELL.LayerParameters for the ELL layer
        layerParameters = ELL.LayerParameters(
            self.layer.ell_inputShape, self.layer.ell_inputPaddingParameters, self.layer.ell_outputShape, self.layer.ell_outputPaddingParameters)

        # Create ELL scaling layer
        if (self.scale.value.size == 1):
            scalesVector = converters.get_float_vector_from_constant(
                self.scale.value, layerParameters.outputShape.channels)
        else:
            scalesVector = converters.get_float_vector_from_cntk_array(
                self.scale.value)

        ellLayers.append(ELL.FloatScalingLayer(
            layerParameters, scalesVector))


class BasePoolingLayer(BaseLayer):
    """Common logic for converting a Pooling layer to ELL"""

    def __init__(self, layer):
        if layer.is_block:
            self.attributes = layer.block_root.attributes
        else:
            self.attributes = layer.attributes
        super().__init__(layer)

    def get_input_padding_parameters(self):
        """Returns the ELL.PaddingParameters for a layer's input."""

        if ('autoPadding' in self.attributes):
            if (self.attributes['autoPadding'][0] == True):
                padding = int(
                    (self.attributes['poolingWindowShape'][0] - 1) / 2)
            else:
                padding = self.attributes['upperPad'][0]
        else:
            padding = self.attributes['upperPad'][0]

        return ELL.PaddingParameters(self.padding_scheme, padding)

    def process(self, ellLayers):
        """Appends the ELL representation of the current layer to ellLayers."""

        # Create the ELL.LayerParameters for the ELL layer
        layerParameters = ELL.LayerParameters(
            self.layer.ell_inputShape, self.layer.ell_inputPaddingParameters, self.layer.ell_outputShape, self.layer.ell_outputPaddingParameters)

        # Fill in the pooling parameters
        poolingSize = self.attributes['poolingWindowShape'][0]
        stride = self.attributes['strides'][0]

        poolingParameters = ELL.PoolingParameters(poolingSize, stride)

        # Create the ELL pooling layer
        ellLayers.append(ELL.FloatPoolingLayer(
            layerParameters, poolingParameters, self.pooling_type))


class MaxPoolingLayer(BasePoolingLayer):
    """Logic for converting a CNTK MaxPooling layer to ELL"""

    def __init__(self, layer):
        self.op_name = 'MaxPooling'
        self.padding_scheme = ELL.PaddingScheme.min
        self.pooling_type = ELL.PoolingType.max

        super().__init__(layer)


class AveragePoolingLayer(BasePoolingLayer):
    """Logic for converting a CNTK AveragePooling layer to ELL"""

    def __init__(self, layer):
        self.op_name = 'AveragePooling'
        self.padding_scheme = ELL.PaddingScheme.zeros
        self.pooling_type = ELL.PoolingType.mean

        super().__init__(layer)


class PoolingLayer(BasePoolingLayer):
    """Logic for converting a CNTK Pooling layer to ELL"""

    def __init__(self, layer):
        self.op_name = 'Pooling'

        super().__init__(layer)

        if (self.attributes['poolingType'] == PoolingType_Max):
            self.actual_layer = AveragePoolingLayer(layer)
        else:
            self.actual_layer = MaxPoolingLayer(layer)

    def process(self, ellLayers):
        """Appends the ELL representation of the current layer to ellLayers."""

        self.actual_layer.process(ellLayers)


class ReLULayer(BaseLayer):
    """Logic for converting a CNTK ReLU layer to ELL"""

    def __init__(self, layer):
        self.op_name = 'ReLU'
        super().__init__(layer)

    def process(self, ellLayers):
        """Appends the ELL representation of the current layer to ellLayers."""

        # Create the ELL.LayerParameters for the ELL layer
        layerParameters = ELL.LayerParameters(
            self.layer.ell_inputShape, self.layer.ell_inputPaddingParameters, self.layer.ell_outputShape, self.layer.ell_outputPaddingParameters)

        # Create the ELL activation layer
        ellLayers.append(ELL.FloatActivationLayer(
            layerParameters, ELL.ActivationType.relu))


class LeakyReLULayer(BaseLayer):
    """Logic for converting a CNTK LeakyReLU layer to ELL"""

    def __init__(self, layer):
        self.op_name = 'LeakyReLU'
        super().__init__(layer)

    def process(self, ellLayers):
        """Appends the ELL representation of the current layer to ellLayers."""

        # Create the ELL.LayerParameters for the ELL layer
        layerParameters = ELL.LayerParameters(
            self.layer.ell_inputShape, self.layer.ell_inputPaddingParameters, self.layer.ell_outputShape, self.layer.ell_outputPaddingParameters)

        # Create the ELL activation layer
        ellLayers.append(ELL.FloatActivationLayer(
            layerParameters, ELL.ActivationType.leaky))


class PReLULayer(BaseLayer):
    """Logic for converting a CNTK PReLU layer to ELL"""

    def __init__(self, layer):
        self.op_name = 'PReLU'
        super().__init__(layer)
        self.prelu_parameter = utilities.find_parameter_by_name(
            self.layer.parameters, 'prelu', 0)

    def process(self, ellLayers):
        """Appends the ELL representation of the current layer to ellLayers."""

        preluTensor = converters.get_float_tensor_from_cntk_convolutional_weight_parameter(
            self.prelu_parameter)

        # Create the ELL.LayerParameters for the ELL layer
        layerParameters = ELL.LayerParameters(
            self.layer.ell_inputShape, self.layer.ell_inputPaddingParameters, self.layer.ell_outputShape, self.layer.ell_outputPaddingParameters)

        # Create the ELL PReLU activation layer
        ellLayers.append(ELL.FloatPReLUActivationLayer(
            layerParameters, preluTensor))


class SoftmaxLayer(BaseLayer):
    """Logic for converting a CNTK Softmax layer to ELL"""

    def __init__(self, layer):
        self.op_name = 'Softmax'
        super().__init__(layer)

    def process(self, ellLayers):
        """Appends the ELL representation of the current layer to ellLayers."""

        if (self.layer.op_name == 'CrossEntropyWithSoftmax'):
            # ugly hack for CrossEntropyWithSoftmax
            # CrossEntropyWithSoftmax outputs to a Tensor[1], but we just need Softmax
            layerParameters = ELL.LayerParameters(
                self.layer.ell_inputShape, self.layer.ell_inputPaddingParameters, self.layer.ell_inputShape, self.layer.ell_inputPaddingParameters)
        else:
            layerParameters = ELL.LayerParameters(
                self.layer.ell_inputShape, self.layer.ell_inputPaddingParameters, self.layer.ell_outputShape, self.layer.ell_outputPaddingParameters)

        # Create the ELL max pooling layer
        ellLayers.append(ELL.FloatSoftmaxLayer(layerParameters))


class BatchNormalizationLayer(BaseLayer):
    """Logic for converting a CNTK BatchNormalization layer to ELL"""

    def __init__(self, layer):
        self.op_name = 'BatchNormalization'

        self.scale = utilities.find_parameter_by_name(
            layer.parameters, 'scale', 0)
        self.bias = utilities.find_parameter_by_name(
            layer.parameters, 'bias', 1)
        self.mean = utilities.find_parameter_by_name(
            layer.constants, 'aggregate_mean', 0)
        self.variance = utilities.find_parameter_by_name(
            layer.constants, 'aggregate_variance', 1)

        # The default CNTK epsilon
        self.epsilon = 1e-5

        super().__init__(layer)

    def process(self, ellLayers):
        """Appends the ELL representation of the current layer to ellLayers."""

        # Note that a single CNTK Batch Normalization layer is equivalent to the following 3 ELL layers:
        # - BatchNormalizationLayer
        # - ScalingLayer
        # - BiasLayer
        #
        # Therefore, make sure the output padding characteristics of the last layer reflect the next layer's
        # padding requirements.

        scaleVector = converters.get_float_vector_from_cntk_trainable_parameter(
            self.scale)
        biasVector = converters.get_float_vector_from_cntk_trainable_parameter(
            self.bias)
        meanVector = converters.get_float_vector_from_cntk_trainable_parameter(
            self.mean)
        varianceVector = converters.get_float_vector_from_cntk_trainable_parameter(
            self.variance)

        # Create the ELL.LayerParameters for the various ELL layers
        firstLayerParameters = ELL.LayerParameters(
            self.layer.ell_inputShape, self.layer.ell_inputPaddingParameters, self.layer.ell_outputShapeMinusPadding, ELL.NoPadding())
        middleLayerParameters = ELL.LayerParameters(self.layer.ell_outputShapeMinusPadding, ELL.NoPadding(
        ), self.layer.ell_outputShapeMinusPadding, ELL.NoPadding())
        lastLayerParameters = ELL.LayerParameters(self.layer.ell_outputShapeMinusPadding, ELL.NoPadding(
        ), self.layer.ell_outputShape, self.layer.ell_outputPaddingParameters)

        # Create the layers
        ellLayers.append(ELL.FloatBatchNormalizationLayer(
            firstLayerParameters, meanVector, varianceVector, self.epsilon, ELL.EpsilonSummand_variance))
        ellLayers.append(ELL.FloatScalingLayer(
            middleLayerParameters, scaleVector))
        ellLayers.append(ELL.FloatBiasLayer(lastLayerParameters, biasVector))


class BiasLayer(BaseLayer):
    """Logic for converting a CNTK Plus layer to ELL"""

    def __init__(self, layer):
        if (len(layer.parameters) != 1):
            raise ValueError(
                "Only processing Plus functions that act as bias layers")

        self.op_name = 'Plus'
        super().__init__(layer)

    def process(self, ellLayers):
        """Appends the ELL representation of the current layer to ellLayers."""

        biasVector = converters.get_float_vector_from_cntk_trainable_parameter(
            self.layer.parameters[0])

        # Create the ELL.LayerParameters for the ELL layer
        layerParameters = ELL.LayerParameters(
            self.layer.ell_inputShape, self.layer.ell_inputPaddingParameters, self.layer.ell_outputShape, self.layer.ell_outputPaddingParameters)

        # Create the ELL bias layer
        ellLayers.append(ELL.FloatBiasLayer(layerParameters, biasVector))


class NegativeBiasLayer(BaseLayer):
    """Logic for converting a CNTK Minus layer to ELL"""

    def __init__(self, layer):
        if (len(layer.constants) != 1 and layer.constants[0].value.size != 1):
            raise ValueError(
                "Skipping Minus function due to dimensions of Constant")

        # TODO: This logic is very fragile, we may want to have a model
        # schema for labeling inputs, nodes, and outputs
        if (layer.output.name != 'mean_removed_input'):
            raise ValueError(
                "Only processing Minus functions that remove input mean")

        self.op_name = 'Minus'
        super().__init__(layer)

    def process(self, ellLayers):
        """Appends the ELL representation of the current layer to ellLayers."""

        # Create the ELL.LayerParameters for the ELL layer
        layerParameters = ELL.LayerParameters(
            self.layer.ell_inputShape, self.layer.ell_inputPaddingParameters, self.layer.ell_outputShape, self.layer.ell_outputPaddingParameters)

        bias = -1.0 * self.layer.constants[0].value
        biasVector = converters.get_float_vector_from_constant(bias, layerParameters.outputShape.channels)

        # Create the ELL bias layer
        ellLayers.append(ELL.FloatBiasLayer(layerParameters, biasVector))


class LayerFactory():
    @staticmethod
    def get_layer_object(cntkLayer):
        try:
            if (cntkLayer.op_name == 'AveragePooling'):
                return AveragePoolingLayer(cntkLayer)
            elif (cntkLayer.op_name == 'BatchNormalization'):
                return BatchNormalizationLayer(cntkLayer)
            elif (cntkLayer.op_name == 'Convolution'):
                if (cntkLayer.is_block):
                    return ConvolutionLayer(cntkLayer)
                else:
                    return BinaryConvolutionLayer(cntkLayer)
            elif (cntkLayer.op_name == 'Dense'):
                return DenseLayer(cntkLayer)
            elif (cntkLayer.op_name == 'ElementTimes'):
                return ElementTimesLayer(cntkLayer)
            elif (cntkLayer.op_name == 'LeakyReLU'):
                return LeakyReLULayer(cntkLayer)
            elif (cntkLayer.op_name == 'linear'):
                return LinearLayer(cntkLayer)
            elif (cntkLayer.op_name == 'MaxPooling'):
                return MaxPoolingLayer(cntkLayer)
            elif (cntkLayer.op_name == 'Minus'):
                return NegativeBiasLayer(cntkLayer)
            elif (cntkLayer.op_name == 'Plus'):
                return BiasLayer(cntkLayer)
            elif (cntkLayer.op_name == 'Pooling'):
                return PoolingLayer(cntkLayer)
            elif (cntkLayer.op_name == 'PReLU'):
                return PReLULayer(cntkLayer)
            elif (cntkLayer.op_name == 'ReLU'):
                return ReLULayer(cntkLayer)
            elif (cntkLayer.op_name == 'Softmax'):
                return SoftmaxLayer(cntkLayer)
            else:
                print("\nWill not process", cntkLayer.op_name,
                      "- skipping this layer as irrelevant.")
        except (ValueError, AttributeError) as e:
            # raised if a layer contains invalid characteristics
            print("\nWill not process", cntkLayer.op_name, "-", str(e))

        return None

    @staticmethod
    def has_inputs(cntkLayer):
        return ((len(cntkLayer.arguments) > 0 and len(cntkLayer.arguments[0].shape) > 0) or
                # special case for Binary Convolution
                (cntkLayer.op_name == 'Convolution' and len(cntkLayer.inputs) > 0 and len(cntkLayer.inputs[0].shape) > 0))


def get_filtered_layers_list(modelLayers, maxLayerCount=None):
    """Returns a relevant list of CNTK layers and layer objects
    """

    # Go through the layers and append layer objects to the relevantLayers list
    relevantLayers = []
    lastSoftmaxLayer = None
    for currentLayer in modelLayers:
        if (isinstance(currentLayer, cntk_py.Function)):
            if (LayerFactory.has_inputs(currentLayer)):
                layerObject = LayerFactory.get_layer_object(currentLayer)
                if (layerObject is not None):
                    relevantLayers.append(layerObject)
                elif currentLayer.op_name == 'CrossEntropyWithSoftmax':
                    # ugly hack for CrossEntropyWithSoftmax
                    # CrossEntropyWithSoftmax pops up in the beginning of the layers list
                    # because the input is connected to it (it's used for evaluating training)
                    lastSoftmaxLayer = SoftmaxLayer(currentLayer)
            else:
                print("\nWill not process", currentLayer.op_name,
                      "- empty input shape.")

    if (lastSoftmaxLayer is not None):
        # Retroactively insert a softmax layer
        relevantLayers.append(lastSoftmaxLayer)

    if (maxLayerCount is not None):
        maxLayerCount = min(maxLayerCount, len(relevantLayers))
        relevantLayers = relevantLayers[0:maxLayerCount]

    # Go through the layers and set the output characteristics:
    # - padding parameters for output, based on the next layer's input
    # - output shape, which is adjusted to include the padding
    currentLayer = None
    for i in range(len(relevantLayers)):
        currentLayer = relevantLayers[i]
        if (i < (len(relevantLayers) - 1)):
            # Use the next layer's input characteristics to set the output for this layer
            nextLayer = relevantLayers[i + 1]
            currentLayer.set_output_characteristics(nextLayer)
        else:
            # This is the last layer, so the output characteristics are known
            currentLayer.set_output_characteristics(None)
        print(currentLayer)

    return relevantLayers


def convert_cntk_layers_to_ell_layers(layersToConvert):
    """Walks a list of CNTK layers and returns a list of ELL Layer objects that is used to construct a Neural Network Predictor"""

    ellLayers = []
    for layerObject in layersToConvert:
        layerObject.process(ellLayers)

    return ellLayers