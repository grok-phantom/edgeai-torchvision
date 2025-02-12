r"""PyTorch Detection Training.

To run in a multi-gpu environment, use the distributed launcher::

    python -m torch.distributed.launch --nproc_per_node=$NGPU --use_env \
        train.py ... --world-size $NGPU

The default hyperparameters are tuned for training on 8 gpus and 2 images per gpu.
    --lr 0.02 --batch-size 2 --world-size 8
If you use different number of gpus, the learning rate should be changed to 0.02/8*$NGPU.

On top of that, for training Faster/Mask R-CNN, the default hyperparameters are
    --epochs 26 --lr-steps 16 22 --aspect-ratio-group-factor 3

Also, if you train Keypoint R-CNN, the default hyperparameters are
    --epochs 46 --lr-steps 36 43 --aspect-ratio-group-factor 3
Because the number of images is smaller in the person keypoint subset of COCO,
the number of epochs should be adapted so that we have the same number of iterations.
"""
import sys
import datetime
import os
import time
from colorama import Fore
import numpy as np

import torch
import torch.utils.data
from torch.utils.tensorboard import SummaryWriter
from tensorboard.program import TensorBoard, tb_logging

import torchvision
import torchvision.models.detection
import torchvision.models.detection.mask_rcnn

from torchvision.edgeailite import xnn


if __name__ == '__main__':
    from coco_utils import get_coco, get_coco_kp, get_detection_modelmaker
    from group_by_aspect_ratio import GroupedBatchSampler, create_aspect_ratio_groups
    from engine import train_one_epoch, evaluate, export, complexity
    import presets
    import utils
    import coco_utils
else:
    from .coco_utils import get_coco, get_coco_kp, get_detection_modelmaker
    from .group_by_aspect_ratio import GroupedBatchSampler, create_aspect_ratio_groups
    from .engine import train_one_epoch, evaluate, export, complexity
    from . import presets
    from . import utils
    from . import coco_utils
#


def get_dataset(name, image_set, transform, data_path, num_classes=None):
    paths = {
        "coco": (data_path, get_coco, 91),
        "coco_kp": (data_path, get_coco_kp, 2),
        "modelmaker": (data_path, get_detection_modelmaker, num_classes+1 if num_classes else 91)
    }
    p, ds_fn, num_classes = paths[name]

    ds = ds_fn(p, image_set=image_set, transforms=transform)
    return ds, num_classes


def get_transform(args, train, data_augmentation):
    '''
    Note: the mean/scale is handled in teh model itself. 
    The mean passed here only for padding.
    '''
    return presets.DetectionPresetTrain(data_augmentation, image_mean=args.image_mean) \
        if train else presets.DetectionPresetEval()


def get_args_parser(add_help=True):
    import argparse
    parser = argparse.ArgumentParser(description='PyTorch Detection Training', add_help=add_help)

    parser.add_argument('--data-path', default='./data/datasets/coco', help='dataset')
    parser.add_argument('--dataset', default='coco', help='dataset')
    parser.add_argument('--model', default='ssdlite_mobilenet_v2_fpn_lite', help='model')
    parser.add_argument('--device', default='cuda', help='device')
    parser.add_argument('-b', '--batch-size', default=4, type=int,
                        help='images per gpu, the total batch size is $NGPU x batch_size')
    parser.add_argument('--epochs', default=60, type=int, metavar='N',
                        help='number of total epochs to run')
    parser.add_argument('-j', '--workers', default=4, type=int, metavar='N',
                        help='number of data loading workers (default: 4)')
    parser.add_argument('--lr', default=0.05, type=float,
                        help='initial learning rate, 0.05 is the default value for training '
                             'on 8 gpus and 2 images_per_gpu')
    parser.add_argument('--momentum', default=0.9, type=float, metavar='M',
                        help='momentum')
    parser.add_argument('--wd', '--weight-decay', default=4e-5, type=float,
                        metavar='W', help='weight decay (default: 4e-5)',
                        dest='weight_decay')
    parser.add_argument('--lr-scheduler', default="cosineannealinglr", help='the lr scheduler (default: cosineannealinglr)')
    parser.add_argument('--lr-step-size', default=8, type=int,
                        help='decrease lr every step-size epochs (multisteplr scheduler only)')
    parser.add_argument('--lr-steps', default=None, nargs='+', type=int,
                        help='decrease lr every step-size epochs (multisteplr scheduler only)')
    parser.add_argument('--lr-gamma', default=0.1, type=float,
                        help='decrease lr by a factor of lr-gamma (multisteplr scheduler only)')
    parser.add_argument('--lr-warmup-epochs', default=5, type=int,
                        help='lr warmup epochs')
    parser.add_argument('--print-freq', default=100, type=int, help='print frequency')
    parser.add_argument('--output-dir', default=None, help='path where to save')
    parser.add_argument('--resume', default='', help='resume from checkpoint')
    parser.add_argument('--start_epoch', '--start-epoch', default=0, type=int, help='start epoch')
    parser.add_argument('--aspect-ratio-group-factor', default=3, type=int)
    parser.add_argument('--rpn-score-thresh', default=None, type=float, help='rpn score threshold for faster-rcnn')
    parser.add_argument('--trainable-backbone-layers', default=None, type=xnn.utils.int_or_none,
                        help='number of trainable layers of backbone')
    parser.add_argument('--data-augmentation', default="ssd", type=xnn.utils.str_or_none, help='data augmentation policy (default: ssd)')
    parser.add_argument(
        "--sync-bn",
        dest="sync_bn",
        help="Use sync batch norm",
        action="store_true",
    )
    parser.add_argument(
        "--test-only",
        dest="test_only",
        help="Only test the model",
        action="store_true",
    )
    parser.add_argument(
        "--pretrained",
        dest="pretrained",
        default=None,
        type=xnn.utils.str_or_bool,
        help="Pre-trained models path or use from from the modelzoo",
    )

    # distributed training parameters
    parser.add_argument('--world-size', default=1, type=int,
                        help='number of distributed processes')
    parser.add_argument('--dist-url', default='env://', help='url used to set up distributed training')
    parser.add_argument("--distributed", default=None, type=xnn.utils.str2bool_or_none,
                        help="use dstributed training even if this script is not launched using torch.disctibuted.launch or run")

    parser.add_argument("--num-classes", default=None, type=xnn.utils.str_to_int, help="number of classes in the dataset")
    parser.add_argument('--gpus', default=1, type=int, help='number of gpus')
    parser.add_argument('--complexity', default=True, type=xnn.utils.str2bool, help='display complexity')
    parser.add_argument('--date', default=datetime.datetime.now().strftime("%Y%m%d-%H%M%S"), help='current date')
    parser.add_argument('--input-size', default=(512,512), type=int, nargs='*', help='resized image size or the smaller side')
    parser.add_argument('--opset-version', default=11, type=int, nargs='*', help='opset version for onnx export')
    parser.add_argument('--resize-with-scale-factor', type=xnn.utils.str2bool, default=True, help='resize with scale factor')
    parser.add_argument('--image-mean', default=(123.675, 116.28, 103.53), type=float, nargs=3, help='mean subtraction of input')
    parser.add_argument('--image-scale', default=(0.017125, 0.017507, 0.017429), type=float, nargs=3, help='scale for multiplication of input')
    parser.add_argument("--tensorboard-logger", default=False, type=xnn.utils.str2bool, help="start tensorboard logging")
    parser.add_argument("--tensorboard-server", default=False, type=xnn.utils.str2bool, help="start tensorboard serving")
    parser.add_argument(
        "--pretrained-backbone",
        dest="pretrained_backbone",
        default=True,
        type=xnn.utils.str_or_bool,
        help="Pre-trained backbone path or use from from the modelzoo",
    )
    parser.add_argument(
        "--export-only",
        dest="export_only",
        help="Only export the model",
        action="store_true",
    )
    parser.add_argument('--save-imgs-path', default=None, type=str, help='if in test-only mode images with output superimposed need saving')
    parser.add_argument('--save-op-txt-path', default=None, type=str, help='if in test-only mode save results in text format')
    parser.add_argument('--max-batches', default=1E9, type=int, help='For debugging if train/test needs exit after few batches.')
    parser.add_argument('--en-wider-face-eval', default=False, type=xnn.utils.str2bool, help='to do additional eval using wider-face method')

    return parser

#load train annotation for bbox drawing for visualizing training images with annotations
def load_train_anno(args=None):
    anno_train = None
    if args.save_imgs_path or args.save_op_txt_path:
        import json
        anno_train = json.load(open(os.path.join(args.data_path, 'annotations', 'instances_train.json')))
    return anno_train

def main(gpu, args):
    if args.device != 'cpu' and args.distributed is True:
        os.environ['RANK'] = str(int(os.environ['RANK'])*args.gpus + gpu) if 'RANK' in os.environ else str(gpu)
        os.environ['LOCAL_RANK'] = str(gpu)

    if args.resize_with_scale_factor:
        torch.nn.functional._interpolate_orig = torch.nn.functional.interpolate
        torch.nn.functional.interpolate = xnn.layers.resize_with_scale_factor

    if args.output_dir is None:
        args.output_dir = os.path.join('./data/checkpoints/detection', f'{args.dataset}_{args.model}')

    utils.mkdir(args.output_dir)
    logger = xnn.utils.TeeLogger(os.path.join(args.output_dir, f'run_{args.date}.log'))

    if args.lr_steps is None:
        args.lr_steps = [int(args.epochs*0.5), int(args.epochs*0.75)]
        # print(f'lr_steps were not specified - using {args.lr_steps}')
    #

    if args.tensorboard_server and utils.is_main_process():
        tb = TensorBoard()
        tb_logging.get_logger().setLevel('ERROR')
        tb.configure(logdir=args.output_dir, bind_all=True)
        tb_url = tb.launch()
        print(f'TensorBoard started at: {tb_url}')
    #

    summary_writer = SummaryWriter(log_dir=args.output_dir) if args.tensorboard_logger and utils.is_main_process() else None

    if args.device != 'cpu':
        utils.init_distributed_mode(args)
    else:
        args.distributed = False

    print(args)
    [print(key, ':', value) for key, value in vars(args).items()]

    device = torch.device(args.device)

    # Data loading code
    print("Loading data")

    dataset, num_classes = get_dataset(args.dataset, "train", get_transform(args, True, args.data_augmentation),
                                       args.data_path, args.num_classes)
    dataset_test, _ = get_dataset(args.dataset, "val", get_transform(args, False, args.data_augmentation), args.data_path)

    print("Creating data loaders")
    if args.distributed:
        train_sampler = torch.utils.data.distributed.DistributedSampler(dataset)
        test_sampler = torch.utils.data.distributed.DistributedSampler(dataset_test)
    else:
        train_sampler = torch.utils.data.RandomSampler(dataset)
        test_sampler = torch.utils.data.SequentialSampler(dataset_test)

    if args.aspect_ratio_group_factor >= 0:
        group_ids = create_aspect_ratio_groups(dataset, k=args.aspect_ratio_group_factor)
        train_batch_sampler = GroupedBatchSampler(train_sampler, group_ids, args.batch_size)
    else:
        train_batch_sampler = torch.utils.data.BatchSampler(
            train_sampler, args.batch_size, drop_last=True)

    data_loader = torch.utils.data.DataLoader(
        dataset, batch_sampler=train_batch_sampler, num_workers=args.workers,
        collate_fn=utils.collate_fn)

    data_loader_test = torch.utils.data.DataLoader(
        dataset_test, batch_size=1,
        sampler=test_sampler, num_workers=args.workers,
        collate_fn=utils.collate_fn)

    print("Creating model")
    kwargs = {
        "trainable_backbone_layers": args.trainable_backbone_layers,
        "size": args.input_size
    }
    if args.export_only:
        kwargs["with_preprocess"] = False
    if args.image_mean is not None:
        # Note: input is divided by 255 before this mean/std is applied
        float_mean = [m/255.0 for m in args.image_mean]
        kwargs.update({"image_mean": float_mean})
    if args.image_scale is not None:
        # Note: this scale/std is applied inside the model, but input is divided by 255 before that
        float_std = [(1.0/s)/255.0 for s in args.image_scale]
        kwargs.update({"image_std": float_std})
    if "rcnn" in args.model:
        if args.rpn_score_thresh is not None:
            kwargs["rpn_score_thresh"] = args.rpn_score_thresh
    model = torchvision.models.detection.__dict__[args.model](num_classes=num_classes, pretrained=args.pretrained,
                                                              pretrained_backbone=args.pretrained_backbone, **kwargs)


    if args.export_only:
        export(args, model, args.model)
        return

    if args.complexity:
        complexity(args, model)

    model.to(device)
    if args.distributed and args.sync_bn:
        model = torch.nn.SyncBatchNorm.convert_sync_batchnorm(model)

    model_without_ddp = model
    if args.distributed:
        model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.gpu])
        model_without_ddp = model.module

    params = [p for p in model.parameters() if p.requires_grad]
    optimizer = torch.optim.SGD(
        params, lr=args.lr, momentum=args.momentum, weight_decay=args.weight_decay)

    args.lr_scheduler = args.lr_scheduler.lower()
    if args.lr_scheduler == 'multisteplr':
        lr_scheduler = xnn.optim.lr_scheduler.MultiStepLRWarmup(optimizer, milestones=args.lr_steps, gamma=args.lr_gamma,
                                                                warmup_epochs=args.lr_warmup_epochs)
    elif args.lr_scheduler == 'cosineannealinglr':
        lr_scheduler = xnn.optim.lr_scheduler.CosineAnnealingLRWarmup(optimizer, T_max=args.epochs,
                                                                      warmup_epochs=args.lr_warmup_epochs)
    else:
        raise RuntimeError("Invalid lr scheduler '{}'. Only MultiStepLR and CosineAnnealingLR "
                           "are supported.".format(args.lr_scheduler))

    if args.resume:
        checkpoint = torch.load(args.resume, map_location='cpu')
        model_without_ddp.load_state_dict(checkpoint['model'])
        optimizer.load_state_dict(checkpoint['optimizer'])
        lr_scheduler.load_state_dict(checkpoint['lr_scheduler'])
        args.start_epoch = checkpoint['epoch'] + 1

    if args.test_only:
        evaluate(args, model, data_loader_test, device=device, epoch=0)
        return

    anno_train = load_train_anno(args=args)
    print("Start training")
    start_time = time.time()
    epoch = 0
    for epoch in range(args.start_epoch, args.epochs):
        if args.distributed:
            train_sampler.set_epoch(epoch)
        train_one_epoch(args, model, optimizer, data_loader, device, epoch, print_freq=args.print_freq, summary_writer=summary_writer, anno=anno_train)
        lr_scheduler.step()

        if args.output_dir:
            checkpoint = {
                'model': model_without_ddp.state_dict(),
                'optimizer': optimizer.state_dict(),
                'lr_scheduler': lr_scheduler.state_dict(),
                'args': args,
                'epoch': epoch
            }
            utils.save_on_master(
                checkpoint,
                os.path.join(args.output_dir, 'model_{}.pth'.format(epoch)))
            utils.save_on_master(
                checkpoint,
                os.path.join(args.output_dir, 'checkpoint.pth'))

        # evaluate after every epoch
        evaluate(args, model, data_loader_test, device=device, epoch=epoch, print_freq=args.print_freq, summary_writer=summary_writer)

        if summary_writer:
            summary_writer.flush()

        if hasattr(args, 'quit_event') and args.quit_event is not None and args.quit_event.is_set():
            break

    total_time = time.time() - start_time
    total_time_str = str(datetime.timedelta(seconds=int(total_time)))
    print('Training time {}'.format(total_time_str))

    if hasattr(args, 'quit_event') and args.quit_event is not None and args.quit_event.is_set():
        return


    print('Model export after training:')
    export(args, model, args.model)

    print('Model training and export completed.')



def run(args):
    if isinstance(args.input_size, (list,tuple)) and len(args.input_size) == 1:
        args.input_size = args.input_size[0]

    if args.device != 'cpu' and args.distributed is True:
        # for explanation of what is happening here, please see this:
        # https://yangkky.github.io/2019/07/08/distributed-pytorch-tutorial.html
        # this assignment of RANK assumes a single machine, but with multiple gpus
        os.environ['RANK'] = '0'
        os.environ['WORLD_SIZE'] = str(args.gpus)
        os.environ['MASTER_ADDR'] = 'localhost'
        os.environ['MASTER_PORT'] = '29500'
        torch.multiprocessing.spawn(main, nprocs=args.gpus, args=(args,))
    else:
        main(0, args)


if __name__ == "__main__":
    args = get_args_parser().parse_args()

    # run the training.
    # if args.distributed is True is set, then this will launch distributed training
    # depending on args.gpus
    run(args)
