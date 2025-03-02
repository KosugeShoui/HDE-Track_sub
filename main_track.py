# Modified by Peize Sun, Rufeng Zhang
# ------------------------------------------------------------------------
# Deformable DETR
# Copyright (c) 2020 SenseTime. All Rights Reserved.
# Licensed under the Apache License, Version 2.0 [see LICENSE for details]
# ------------------------------------------------------------------------
# Modified from DETR (https://github.com/facebookresearch/detr)
# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
# ------------------------------------------------------------------------
import argparse
import datetime
import json
import random
import time
from pathlib import Path

import numpy as np
import torch
from torch.utils.data import DataLoader
import datasets
import util.misc as utils
import datasets.samplers as samplers
from datasets.sampler_video_distributed import DistributedVideoSampler
from datasets import build_dataset, get_coco_api_from_dataset
from engine_track import evaluate, train_one_epoch, multiply_loss_giou_values, sigmoid_base_sche, sigmoid
from models import build_tracktrain_model, build_tracktest_model, build_model
from models import Tracker
from models import save_track

from collections import defaultdict
from tqdm import tqdm
from learning_curve_each import plot_combined_loss
from learning_unscaled_curve_each import plot_combined_unscaled_loss
from ultralytics import RTDETR


def get_args_parser():
    parser = argparse.ArgumentParser('Deformable DETR Detector', add_help=False)
    parser.add_argument('--lr', default=2e-4, type=float)
    parser.add_argument('--lr_backbone_names', default=["backbone.0"], type=str, nargs='+')
    parser.add_argument('--lr_backbone', default=2e-5, type=float)
    parser.add_argument('--lr_linear_proj_names', default=['reference_points', 'sampling_offsets'], type=str, nargs='+')
    parser.add_argument('--lr_linear_proj_mult', default=0.1, type=float)
    parser.add_argument('--batch_size', default=1, type=int)
    parser.add_argument('--weight_decay', default=1e-4, type=float)
    parser.add_argument('--final_weight', default=0.1, type=float)
    parser.add_argument('--epochs', default=50, type=int)
    parser.add_argument('--lr_drop', default=40, type=int)
    parser.add_argument('--lr_drop_epochs', default=None, type=int, nargs='+')
    parser.add_argument('--clip_max_norm', default=0.1, type=float,
                        help='gradient clipping max norm')
    
    #スケジューリング導入するかどうか
    parser.add_argument('--loss_schedule', default=False, action='store_true')
    #parser.add_argument('--time_sche', default=False, action='store_true')
    parser.add_argument('--timesformer',default=False,action='store_true')

    parser.add_argument('--sgd', action='store_true')

    # Variants of Deformable DETR
    parser.add_argument('--with_box_refine', default=True, action='store_true')
    parser.add_argument('--two_stage', default=True, action='store_true')

    # Model parameters
    parser.add_argument('--frozen_weights', type=str, default=None,
                        help="Path to the pretrained model. If set, only the mask head will be trained")

    # * Backbone
    parser.add_argument('--backbone', default='resnet50', type=str,
                        help="Name of the convolutional backbone to use")
    parser.add_argument('--dilation', action='store_true',
                        help="If true, we replace stride with dilation in the last convolutional block (DC5)")
    parser.add_argument('--position_embedding', default='sine', type=str, choices=('sine', 'learned'),
                        help="Type of positional embedding to use on top of the image features")
    parser.add_argument('--position_embedding_scale', default=2 * np.pi, type=float,
                        help="position / size * scale")
    parser.add_argument('--num_feature_levels', default=4, type=int, help='number of feature levels')

    # * Transformer
    parser.add_argument('--enc_layers', default=6, type=int,
                        help="Number of encoding layers in the transformer")
    parser.add_argument('--dec_layers', default=6, type=int,
                        help="Number of decoding layers in the transformer")
    parser.add_argument('--dim_feedforward', default=1024, type=int,
                        help="Intermediate size of the feedforward layers in the transformer blocks")
    parser.add_argument('--hidden_dim', default=256, type=int,
                        help="Size of the embeddings (dimension of the transformer)")
    parser.add_argument('--dropout', default=0.1, type=float,
                        help="Dropout applied in the transformer")
    parser.add_argument('--nheads', default=8, type=int,
                        help="Number of attention heads inside the transformer's attentions")
    parser.add_argument('--num_queries', default=500, type=int,
                        help="Number of query slots")
    parser.add_argument('--dec_n_points', default=4, type=int)
    parser.add_argument('--enc_n_points', default=4, type=int)

    # * Segmentation
    parser.add_argument('--masks', action='store_true',
                        help="Train segmentation head if the flag is provided")

    # Loss
    parser.add_argument('--no_aux_loss', dest='aux_loss', action='store_false',
                        help="Disables auxiliary decoding losses (loss at each layer)")

    # * Matcher
    parser.add_argument('--set_cost_class', default=2, type=float,
                        help="Class coefficient in the matching cost")
    parser.add_argument('--set_cost_bbox', default=5, type=float,
                        help="L1 box coefficient in the matching cost")
    parser.add_argument('--set_cost_giou', default=2, type=float,
                        help="giou box coefficient in the matching cost")

    # * Loss coefficients
    parser.add_argument('--mask_loss_coef', default=1, type=float)
    parser.add_argument('--dice_loss_coef', default=1, type=float)
    parser.add_argument('--cls_loss_coef', default=2, type=float)
    parser.add_argument('--bbox_loss_coef', default=5, type=float)
    parser.add_argument('--giou_loss_coef', default=2, type=float)
    parser.add_argument('--focal_alpha', default=0.25, type=float)
    parser.add_argument('--id_loss_coef', default=1, type=float)

    # dataset parameters
    parser.add_argument('--dataset_file', default='visem')
    parser.add_argument('--coco_path', default='./data/coco', type=str)
    parser.add_argument('--coco_panoptic_path', type=str)
    parser.add_argument('--remove_difficult', action='store_true')

    parser.add_argument('--output_dir', default='',
                        help='path where to save, empty for no saving')
    parser.add_argument('--device', default='cuda',
                        help='device to use for training / testing')
    parser.add_argument('--seed', default=42, type=int)
    parser.add_argument('--resume', default='', help='resume from checkpoint')
    parser.add_argument('--resume_detr', default='', help='resume from checkpoint')
    parser.add_argument('--start_epoch', default=0, type=int, metavar='N',
                        help='start epoch')
    parser.add_argument('--eval', action='store_true')
    parser.add_argument('--num_workers', default=1, type=int)
    parser.add_argument('--cache_mode', default=False, action='store_true', help='whether to cache images on memory')

    # PyTorch checkpointing for saving memory (torch.utils.checkpoint.checkpoint)
    parser.add_argument('--checkpoint_enc_ffn', default=False, action='store_true')
    parser.add_argument('--checkpoint_dec_ffn', default=False, action='store_true')

    # appended for track.
    parser.add_argument('--track_train_split', default='train', type=str)
    #parser.add_argument('--track_eval_split', default='val', type=str)
    parser.add_argument('--track_eval_split', default='test', type=str)
    parser.add_argument('--track_thresh', default=0.4, type=float)
    parser.add_argument('--reid_shared', default=False, type=bool)
    parser.add_argument('--reid_dim', default=128, type=int)
    parser.add_argument('--num_ids', default=360, type=int)
    
    
    # detector for track.
    parser.add_argument('--det_val', default=False, action='store_true')

    # fp16
    parser.add_argument('--fp16', default=False, action='store_true')
    
    # multi-gpu test
    parser.add_argument('--start_id', default = 0, type=int)
    parser.add_argument('--dist_video', default=False, action='store_true')
    return parser


def main(args):

    utils.init_distributed_mode(args)
    #print("git:\n  {}\n".format(utils.get_sa()))
    #print(args.gpu)

    if args.frozen_weights is not None:
        assert args.masks, "Frozen training is meant for segmentation only"
    print(args)

    device = torch.device(args.device)

    # fix the seed for reproducibility
    seed = args.seed + utils.get_rank()
    torch.manual_seed(seed)
    np.random.seed(seed)
    random.seed(seed)
    
    scaler = torch.cuda.amp.GradScaler(enabled=args.fp16)
    if args.det_val:
        assert args.eval, 'only support eval mode of detector for track'
        model, criterion, postprocessors = build_model(args)
    elif args.eval:
        model, criterion, postprocessors = build_tracktest_model(args)
    else:
        model, criterion, postprocessors = build_tracktrain_model(args)
        
    model.to(device)

    model_without_ddp = model
    #ここでパラメータ✓、勾配更新されているものだけのパラメータ
    #n_parameters = sum(p.numel() for p in model.parameters())
    n_parameters = sum(p.numel() for p in model.parameters() if p.requires_grad)
    print('number of params:', n_parameters)
     
    # ----------- DETRモデルの定義 ----------
    yolo_model = RTDETR('models/pretrain_rtdetr_q500.pt')
    #print('device =  ',device)
    yolo_model.to(device)
    #yolo_model = RTDETR('rtdetr-l.pt')
    # モデルのすべてのパラメータを学習可能 or 不可能に設定
    for param in yolo_model.parameters():
        param.requires_grad = False
    params = 0
    for n, p in yolo_model.named_parameters():
        if p.requires_grad:
            #print('Detection Model grad_norm True layer = ',n)
            params += p.numel()
    print('DETR learnable params = ',params)
    print('--------------------------------------------------')
    
    # モデルのパラメータを固定する
    

    # ---------------------------------------

    dataset_train = build_dataset(image_set=args.track_train_split, args=args)
    dataset_val = build_dataset(image_set=args.track_eval_split, args=args)
    
    #check
    #args.distributed = False

    if args.distributed:
        if args.cache_mode:
            sampler_train = samplers.NodeDistributedSampler(dataset_train)
            sampler_val = samplers.NodeDistributedSampler(dataset_val, shuffle=False)
        else:
            sampler_train = samplers.DistributedSampler(dataset_train)
            if args.dist_video:
                #print('ok')
                sampler_val = DistributedVideoSampler(dataset_val, start_id=args.start_id, shuffle=False)
            else:
                sampler_val = samplers.DistributedSampler(dataset_val, shuffle=False)     
    else:
        sampler_train = torch.utils.data.RandomSampler(dataset_train)
        sampler_val = torch.utils.data.SequentialSampler(dataset_val)

    batch_sampler_train = torch.utils.data.BatchSampler(
        sampler_train, args.batch_size, drop_last=True)

    data_loader_train = DataLoader(dataset_train, batch_sampler=batch_sampler_train,
                                   collate_fn=utils.collate_fn, num_workers=args.num_workers,
                                   pin_memory=True)
    data_loader_val = DataLoader(dataset_val, args.batch_size, sampler=sampler_val,
                                 drop_last=False, collate_fn=utils.collate_fn, num_workers=args.num_workers,
                                 pin_memory=True)

    # lr_backbone_names = ["backbone.0", "backbone.neck", "input_proj", "transformer.encoder"]
    def match_name_keywords(n, name_keywords):
        out = False
        for b in name_keywords:
            if b in n:
                out = True
                break
        return out

    for n, p in model_without_ddp.named_parameters():
        if p.requires_grad:
            print('grad_norm true layer = ',n)

    param_dicts = [
        {
            "params":
                [p for n, p in model_without_ddp.named_parameters()
                 if not match_name_keywords(n, args.lr_backbone_names) and not match_name_keywords(n, args.lr_linear_proj_names) and p.requires_grad],
            "lr": args.lr,
        },
        {
            "params": [p for n, p in model_without_ddp.named_parameters() if match_name_keywords(n, args.lr_backbone_names) and p.requires_grad],
            "lr": args.lr_backbone,
        },
        {
            "params": [p for n, p in model_without_ddp.named_parameters() if match_name_keywords(n, args.lr_linear_proj_names) and p.requires_grad],
            "lr": args.lr * args.lr_linear_proj_mult,
        }
    ]
    if args.sgd:
        optimizer = torch.optim.SGD(param_dicts, lr=args.lr, momentum=0.9,
                                    weight_decay=args.weight_decay)
    else:
        optimizer = torch.optim.AdamW(param_dicts, lr=args.lr,
                                      weight_decay=args.weight_decay)
    lr_scheduler = torch.optim.lr_scheduler.StepLR(optimizer, args.lr_drop)

    if args.distributed:
        print('---------- distribution ----------')
        #args.gpu = 2
        #print(args.gpu) 
        model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.gpu], find_unused_parameters=True)
        model_without_ddp = model.module

    if args.dataset_file == "coco_panoptic":
        # We also evaluate AP during panoptic training, on original coco DS
        coco_val = datasets.coco.build("val", args)
        base_ds = get_coco_api_from_dataset(coco_val)
    else:
        base_ds = get_coco_api_from_dataset(dataset_val)

    if args.frozen_weights is not None:
        checkpoint = torch.load(args.frozen_weights, map_location='cpu')
        model_without_ddp.detr.load_state_dict(checkpoint['model'])

    output_dir = Path(args.output_dir)
    if args.resume:
        print('resume use true')
        if args.resume.startswith('https'):
            checkpoint = torch.hub.load_state_dict_from_url(
                args.resume, map_location='cpu', check_hash=True)
        else:
            checkpoint = torch.load(args.resume, map_location='cpu')
        
        missing_keys, unexpected_keys = model_without_ddp.load_state_dict(checkpoint['model'], strict=False)
        unexpected_keys = [k for k in unexpected_keys if not (k.endswith('total_params') or k.endswith('total_ops'))]
        
        
        if len(missing_keys) > 0:
            print('Missing Keys: {}'.format(missing_keys))
        if len(unexpected_keys) > 0:
            print('Unexpected Keys: {}'.format(unexpected_keys))
        if not args.eval and 'optimizer' in checkpoint and 'lr_scheduler' in checkpoint and 'epoch' in checkpoint:
            import copy
            p_groups = copy.deepcopy(optimizer.param_groups)
            optimizer.load_state_dict(checkpoint['optimizer'])
            for pg, pg_old in zip(optimizer.param_groups, p_groups):
                pg['lr'] = pg_old['lr']
                pg['initial_lr'] = pg_old['initial_lr']
            #print(optimizer.param_groups)
            lr_scheduler.load_state_dict(checkpoint['lr_scheduler'])
            # todo: this is a hack for doing experiment that resume from checkpoint and also modify lr scheduler (e.g., decrease lr in advance).
            args.override_resumed_lr_drop = True
            if args.override_resumed_lr_drop:
                #print('Warning: (hack) args.override_resumed_lr_drop is set to True, so args.lr_drop would override lr_drop in resumed lr_scheduler.')
                lr_scheduler.step_size = args.lr_drop
                lr_scheduler.base_lrs = list(map(lambda group: group['initial_lr'], optimizer.param_groups))
            lr_scheduler.step(lr_scheduler.last_epoch)
            args.start_epoch = checkpoint['epoch'] + 1
        # check the resumed model
#         if not args.eval:
#             test_stats, coco_evaluator, _ = evaluate(
#                 model, criterion, postprocessors, data_loader_val, base_ds, device, args.output_dir
#             )
    
    if args.eval:
        assert args.batch_size == 1, print("Now only support 1.")
        tracker = Tracker(score_thresh=args.track_thresh)
        #checkpoint_detr = torch.load(args.resume_detr, map_location='cpu')
        
        #print(checkpoint_detr['model'].keys())
        
        yolo_model_eval = RTDETR('models/pretrain_rtdetr_q100.pt')
        #yolo_model_eval.load_state_dict(checkpoint_detr['model'],strict=False)
        # モデルにロードされたパラメータ数（学習可能パラメータのみカウント）
        num_params = sum(p.numel() for p in yolo_model.parameters() if p.requires_grad_ == False)
        print('--------------------------------')
        print(f"DETR eval Params : {num_params}")
        print('--------------------------------')

        
        #print('DETR params = ',len(checkpoint_detr['model']))
            
        test_stats, coco_evalu_ator, res_tracks = evaluate(model, yolo_model_eval, criterion, postprocessors, data_loader_val,
                                                          base_ds, device, args.output_dir, tracker=tracker, 
                                                          phase='eval', det_val=args.det_val, fp16=args.fp16)
        if args.output_dir:
#             utils.save_on_master(coco_evaluator.coco_eval["bbox"].eval, output_dir / "eval.pth")
            if res_tracks is not None:
                print("Creating video index for {}.".format(args.dataset_file))
                video_to_images = defaultdict(list)
                video_names = defaultdict()
                
                coco = dataset_val.coco
                img_idxs = sampler_val.indices[utils.get_rank()] if args.distributed else list(range(len(dataset_val)))
                
#                 for _, img_info in dataset_val.coco.imgs.items():
                for i, idx in enumerate(img_idxs):
                    img_id = dataset_val.ids[idx]
                    img_info = coco.loadImgs(img_id)[0]
                    
                    video_id = img_info["video_id"]
                    video_to_images[video_id].append({"image_id": img_info["id"], "frame_id": img_info["frame_id"]})
                    video_name = img_info["file_name"].split("/")[0]
                    if video_id not in video_names:
                        video_names[video_id] = video_name
                

                assert len(video_to_images) == len(video_names)
                # save mot results.
                save_track(res_tracks, args.output_dir, video_to_images, video_names, args.track_eval_split)

        return

    print("--------------------Start training--------------------\n")
    #print(args.start_epoch)
    #print('epoch = ',epoch + 1)
    start_time = time.time()
     # ベストepochのためのloss_dictの定義
    loss_list = []
    
    #
    #for name, param in model.named_parameters():
        #if "decoder.layers" in name:  # decoder.layersという名前を含むパラメータを特定
            #param.requires_grad = False
            #print(f"Froze parameter: {name}")
            
    for epoch in tqdm(range(args.start_epoch, args.epochs)):
        if args.distributed:
            sampler_train.set_epoch(epoch)

    
        
        if args.loss_schedule :
            print('\n --- loss schedule true --- \n')
            #重みのスケジュール導入 : sigmoid base 
            #multi_weight = exponential_decay(args.set_cost_giou,args.final_weight,args.epochs)
            multi_weight = sigmoid_base_sche(args.set_cost_giou,args.final_weight,args.epochs)
            new_weight_dict = multiply_loss_giou_values(criterion.weight_dict,multi_weight[epoch])
            #print(multi_weight)   
        else:
            new_weight_dict = criterion.weight_dict
        
        
        
        #learning start 
        train_stats = train_one_epoch(
            model,yolo_model, criterion, data_loader_train, optimizer, device, scaler, epoch,new_weight_dict,args.clip_max_norm, fp16=args.fp16)
        
        lr_scheduler.step()
                
        log_stats = {**{f'train_{k}': v for k, v in train_stats.items()},
                     'epoch': epoch,
                     'n_parameters': n_parameters}
        
        if args.output_dir and utils.is_main_process():
            with (output_dir / "log.txt").open("a") as f:
                f.write(json.dumps(log_stats) + "\n")
        
        with open((output_dir / "log.txt"), 'r') as file:
            lines = file.readlines()

        json_txt = lines[len(lines)-1]
        data = json.loads(json_txt)
        loss_list.append(data['train_loss'])
        
        # Best Epoch save phase
        if args.output_dir:
            checkpoint_paths = [output_dir / 'checkpoint.pth']
            #checkpoint_detr_paths = [output_dir / 'checkpoint_detr.pth']
            # extra checkpoint before LR drop and every 5 epochs
            if (epoch + 1) % args.lr_drop == 0 or (epoch + 1) % 5 == 0:
                checkpoint_paths.append(output_dir / f'checkpoint{epoch + 1:02}.pth')
                #checkpoint_detr_paths.append(output_dir / f'checkpoint_detr{epoch + 1:02}.pth')
            # 最終epochの重み保存
            if epoch + 1 == args.epochs:
                checkpoint_paths.append(output_dir / f'checkpoint{args.epochs:02}.pth')
                #checkpoint_detr_paths.append(output_dir / f'checkpoint_detr{args.epochs:02}.pth')
            
            for checkpoint_path in checkpoint_paths:
                utils.save_on_master({
                    'model': model_without_ddp.state_dict(),
                    'optimizer': optimizer.state_dict(),
                    'lr_scheduler': lr_scheduler.state_dict(),
                    'epoch': epoch,
                    'args': args,
                }, checkpoint_path)
                
            # DETR用の重み保存   
            """ 
            for checkpoint_path in checkpoint_detr_paths:
                utils.save_on_master({
                    'model': yolo_model.state_dict(),
                    'optimizer': optimizer.state_dict(),
                    'lr_scheduler': lr_scheduler.state_dict(),
                    'epoch': epoch,
                    'args': args,
                }, checkpoint_path)
            """
        
        #1エポックごとに学習曲線を保存
        plot_combined_loss(args.output_dir)
        plot_combined_unscaled_loss(args.output_dir)
        #plot_unscaled_combined_loss(args.output_dir)
        #1エポックここまで
    total_time = time.time() - start_time
    total_time_str = str(datetime.timedelta(seconds=int(total_time)))
    print('\n ---------- Training time {} -----------'.format(total_time_str))


if __name__ == '__main__':
    parser = argparse.ArgumentParser('Deformable DETR training and evaluation script', parents=[get_args_parser()])
    args = parser.parse_args()
    if args.output_dir:
        Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    main(args)
