import torch
import warnings

from collections import OrderedDict
from functools import partial
from torch import nn, Tensor
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import _utils as det_utils
from .ssd import SSD, SSDScoringHead
from .anchor_utils import DefaultBoxGenerator
from .backbone_utils import _validate_trainable_layers
from .. import mobilenet
from ..._internally_replaced_utils import load_state_dict_from_url
from ...ops.misc import ConvNormActivation
from ...edgeailite import xnn


__all__ = ['ssdlite320_mobilenet_v3_large', 'ssdlite_mobilenet_v3_large']

model_urls = {
    'ssdlite320_mobilenet_v3_large_coco':
        'https://download.pytorch.org/models/ssdlite320_mobilenet_v3_large_coco-a79551df.pth'
}


# Building blocks of SSDlite as described in section 6.2 of MobileNetV2 paper
def _prediction_block(in_channels: int, out_channels: int, kernel_size: int,
                      norm_layer: Callable[..., nn.Module]) -> nn.Sequential:
    return nn.Sequential(
        # 3x3 depthwise with stride 1 and padding 1
        ConvNormActivation(in_channels, in_channels, kernel_size=kernel_size, groups=in_channels,
                           norm_layer=norm_layer, activation_layer=nn.ReLU6),

        # 1x1 projetion to output channels
        nn.Conv2d(in_channels, out_channels, 1)
    )


def _extra_block(in_channels: int, out_channels: int, norm_layer: Callable[..., nn.Module]) -> nn.Sequential:
    activation = nn.ReLU6
    intermediate_channels = out_channels // 2
    return nn.Sequential(
        # 1x1 projection to half output channels
        ConvNormActivation(in_channels, intermediate_channels, kernel_size=1,
                           norm_layer=norm_layer, activation_layer=activation),

        # 3x3 depthwise with stride 2 and padding 1
        ConvNormActivation(intermediate_channels, intermediate_channels, kernel_size=3, stride=2,
                           groups=intermediate_channels, norm_layer=norm_layer, activation_layer=activation),

        # 1x1 projetion to output channels
        ConvNormActivation(intermediate_channels, out_channels, kernel_size=1,
                           norm_layer=norm_layer, activation_layer=activation),
    )


def _normal_init(conv: nn.Module):
    for layer in conv.modules():
        if isinstance(layer, nn.Conv2d):
            torch.nn.init.normal_(layer.weight, mean=0.0, std=0.03)
            if layer.bias is not None:
                torch.nn.init.constant_(layer.bias, 0.0)


class SSDLiteHead(nn.Module):
    def __init__(self, in_channels: List[int], num_anchors: List[int], num_classes: int,
                 norm_layer: Callable[..., nn.Module]):
        super().__init__()
        self.classification_head = SSDLiteClassificationHead(in_channels, num_anchors, num_classes, norm_layer)
        self.regression_head = SSDLiteRegressionHead(in_channels, num_anchors, norm_layer)
        self.num_classes = num_classes

    def forward(self, x: List[Tensor]) -> Dict[str, Tensor]:
        return {
            'bbox_regression': self.regression_head(x),
            'cls_logits': self.classification_head(x),
        }


class SSDLiteClassificationHead(SSDScoringHead):
    def __init__(self, in_channels: List[int], num_anchors: List[int], num_classes: int,
                 norm_layer: Callable[..., nn.Module]):
        cls_logits = nn.ModuleList()
        for channels, anchors in zip(in_channels, num_anchors):
            cls_logits.append(_prediction_block(channels, num_classes * anchors, 3, norm_layer))
        _normal_init(cls_logits)
        super().__init__(cls_logits, num_classes)


class SSDLiteRegressionHead(SSDScoringHead):
    def __init__(self, in_channels: List[int], num_anchors: List[int], norm_layer: Callable[..., nn.Module]):
        bbox_reg = nn.ModuleList()
        for channels, anchors in zip(in_channels, num_anchors):
            bbox_reg.append(_prediction_block(channels, 4 * anchors, 3, norm_layer))
        _normal_init(bbox_reg)
        super().__init__(bbox_reg, 4)


class SSDLiteFeatureExtractorMobileNet(nn.Module):
    def __init__(self, backbone: nn.Module, c4_pos: int, norm_layer: Callable[..., nn.Module], width_mult: float = 1.0,
                 min_depth: int = 16, **kwargs: Any):
        super().__init__()

        assert not backbone[c4_pos].use_res_connect
        self.features = nn.Sequential(
            # As described in section 6.3 of MobileNetV3 paper
            nn.Sequential(*backbone[:c4_pos], backbone[c4_pos].block[0]),  # from start until C4 expansion layer
            nn.Sequential(backbone[c4_pos].block[1:], *backbone[c4_pos + 1:]),  # from C4 depthwise until end
        )

        get_depth = lambda d: max(min_depth, int(d * width_mult))  # noqa: E731
        extra = nn.ModuleList([
            _extra_block(backbone[-1].out_channels, get_depth(512), norm_layer),
            _extra_block(get_depth(512), get_depth(256), norm_layer),
            _extra_block(get_depth(256), get_depth(256), norm_layer),
            _extra_block(get_depth(256), get_depth(128), norm_layer),
        ])
        _normal_init(extra)

        self.extra = extra

    def forward(self, x: Tensor) -> Dict[str, Tensor]:
        # Get feature maps from backbone and extra. Can't be refactored due to JIT limitations.
        output = []
        for block in self.features:
            x = block(x)
            output.append(x)

        for block in self.extra:
            x = block(x)
            output.append(x)

        return OrderedDict([(str(i), v) for i, v in enumerate(output)])


def _mobilenet_extractor(backbone_name: str, progress: bool, pretrained: bool, trainable_layers: int,
                         norm_layer: Callable[..., nn.Module], **kwargs: Any):
    backbone = mobilenet.__dict__[backbone_name](pretrained=pretrained, progress=progress,
                                                 norm_layer=norm_layer, **kwargs).features
    if not pretrained:
        # Change the default initialization scheme if not pretrained
        _normal_init(backbone)

    # Gather the indices of blocks which are strided. These are the locations of C1, ..., Cn-1 blocks.
    # The first and last blocks are always included because they are the C0 (conv1) and Cn.
    stage_indices = [0] + [i for i, b in enumerate(backbone) if getattr(b, "_is_cn", False)] + [len(backbone) - 1]
    num_stages = len(stage_indices)

    # find the index of the layer from which we wont freeze
    assert 0 <= trainable_layers <= num_stages
    freeze_before = len(backbone) if trainable_layers == 0 else stage_indices[num_stages - trainable_layers]

    for b in backbone[:freeze_before]:
        for parameter in b.parameters():
            parameter.requires_grad_(False)

    return SSDLiteFeatureExtractorMobileNet(backbone, stage_indices[-2], norm_layer, **kwargs)


def ssdlite320_mobilenet_v3_large(pretrained: bool = False, progress: bool = True, num_classes: int = 91,
                                  pretrained_backbone: bool = False, trainable_backbone_layers: Optional[int] = None,
                                  norm_layer: Optional[Callable[..., nn.Module]] = None,
                                  size: Optional[Tuple] = None,
                                  backbone_name = None, 
                                  reduce_tail = False,								  
								  weights_name = 'ssdlite320_mobilenet_v3_large_coco',
                                  **kwargs: Any):
    """Constructs an SSDlite model with input size 320x320 and a MobileNetV3 Large backbone, as described at
    `"Searching for MobileNetV3"
    <https://arxiv.org/abs/1905.02244>`_ and
    `"MobileNetV2: Inverted Residuals and Linear Bottlenecks"
    <https://arxiv.org/abs/1801.04381>`_.

    See :func:`~torchvision.models.detection.ssd300_vgg16` for more details.

    Example:

        >>> model = torchvision.models.detection.ssdlite320_mobilenet_v3_large(pretrained=True)
        >>> model.eval()
        >>> x = [torch.rand(3, 320, 320), torch.rand(3, 500, 400)]
        >>> predictions = model(x)

    Args:
        pretrained (bool): If True, returns a model pre-trained on COCO train2017
        progress (bool): If True, displays a progress bar of the download to stderr
        num_classes (int): number of output classes of the model (including the background)
        pretrained_backbone (bool): If True, returns a model with backbone pre-trained on Imagenet
        trainable_backbone_layers (int): number of trainable (not frozen) resnet layers starting from final block.
            Valid values are between 0 and 6, with 6 meaning all backbone layers are trainable.
        norm_layer (callable, optional): Module specifying the normalization layer to use.
    """
    if size is None:
        warnings.warn("The size of the model is not provided; using default.")
        size = (320, 320)

    trainable_backbone_layers = _validate_trainable_layers(
        pretrained or pretrained_backbone, trainable_backbone_layers, 6, 6)

    if pretrained:
        pretrained_backbone = False

    # Enable reduced tail if no pretrained backbone is selected. See Table 6 of MobileNetV3 paper.
    # reduce_tail = not pretrained_backbone

    if norm_layer is None:
        norm_layer = partial(nn.BatchNorm2d, eps=0.001, momentum=0.03)

    backbone = _mobilenet_extractor(backbone_name, progress, pretrained_backbone, trainable_backbone_layers,
                                    norm_layer, reduced_tail=reduce_tail, **kwargs)

    anchor_generator = DefaultBoxGenerator([[2, 3] for _ in range(6)], min_ratio=0.2, max_ratio=0.95)
    out_channels = det_utils.retrieve_out_channels(backbone, size)
    num_anchors = anchor_generator.num_anchors_per_location()
    assert len(out_channels) == len(anchor_generator.aspect_ratios)

    defaults = {
        "score_thresh": 0.001,
        "nms_thresh": 0.55,
        "detections_per_img": 300,
        "topk_candidates": 300,
        # If image_mean or image_std is in kwargs, use it, otherwise these defaults will take effect
        # The following default mean/std rescale the input in a way compatible to the (tensorflow?) backbone:
        # i.e. rescale the data in [0, 1] to [-1, -1]. But this is different from typical torchvision backbones.
        "image_mean": kwargs.pop('image_mean', [0.5, 0.5, 0.5]),
        "image_std":  kwargs.pop('image_std', [0.5, 0.5, 0.5]),
    }
    kwargs = {**defaults, **kwargs}
    model = SSD(backbone, anchor_generator, size, num_classes,
                head=SSDLiteHead(out_channels, num_anchors, num_classes, norm_layer), **kwargs)

    if pretrained is True:
        if model_urls.get(weights_name, None) is None:
            raise ValueError("No checkpoint is available for model {}".format(weights_name))
        state_dict = load_state_dict_from_url(model_urls[weights_name], progress=progress)
        model.load_state_dict(state_dict)
    elif xnn.utils.is_url(pretrained):
        state_dict = load_state_dict_from_url(pretrained, progress=progress)
        _load_state_dict(model, state_dict)
    elif isinstance(pretrained, str):
        state_dict = torch.load(pretrained)
        _load_state_dict(model, state_dict)
    return model


def _load_state_dict(model, state_dict):
    state_dict = state_dict['model'] if 'model' in state_dict else state_dict
    state_dict = state_dict['state_dict'] if 'state_dict' in state_dict else state_dict
    try:
        model.load_state_dict(state_dict)
    except:
        model.load_state_dict(state_dict, strict=False)


def ssdlite_mobilenet_v3_large(*args, backbone_name="mobilenet_v3_large", **kwargs):
    return ssdlite320_mobilenet_v3_large(*args, backbone_name=backbone_name, weights_name=ssdlite_mobilenet_v3_large, **kwargs)

