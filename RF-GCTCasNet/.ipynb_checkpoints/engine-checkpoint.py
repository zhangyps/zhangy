import math
import sys
from copy import deepcopy
import os
import torch
from torch.nn.utils import clip_grad_norm_
from tqdm import tqdm

from eval_func import eval_detection, eval_search_cuhk, eval_search_prw
from utils.utils import MetricLogger, SmoothedValue, mkdir, reduce_dict, warmup_lr_scheduler, write_text
from utils.transforms import mixup_data

def to_device(images, targets, device):
    images = [image.to(device) for image in images]
    for t in targets:
        t["boxes"] = t["boxes"].to(device)
        t["labels"] = t["labels"].to(device)
    return images, targets


def train_one_epoch(cfg, model, optimizer, data_loader, device, epoch, tfboard, softmax_criterion_s2, softmax_criterion_s3, outsys_dir = None):
    model.train()
    #度量记录器
    #Metes用于产生带有默认值的dict，MetricLogger两个属性，self.meters——初始化利用SmoothValue类， self.delimiter——字符串类型，用来更新数值
    metric_logger = MetricLogger(delimiter="  ", txt_dir=os.path.join(outsys_dir, 'os.txt'))
    metric_logger.add_meter("lr", SmoothedValue(window_size=1, fmt="{value:.6f}"))
    header = "Epoch: [{}]".format(epoch)
    write_text(sentence="Epoch: [{}]".format(epoch), fpath=os.path.join(outsys_dir, 'os.txt'), print_on=False)

    # warmup learning rate in the first epoch
    if epoch == 0:
        warmup_factor = 1.0 / 1000
        # FIXME: min(1000, len(data_loader) - 1)
        warmup_iters = len(data_loader) - 1
        warmup_scheduler = warmup_lr_scheduler(optimizer, warmup_iters, warmup_factor)

    for i, (images, targets) in enumerate(
        metric_logger.log_every(data_loader, cfg.DISP_PERIOD, header)   #训练多少iterms进行display
    ):
        images, targets = to_device(images, targets, device)
        if cfg.INPUT.IMAGE_MIXUP:
            images = mixup_data(images, alpha=0.8)
#######!!!!!
        loss_dict, feats_reid_2nd, targets_reid_2nd, feats_reid_3rd, targets_reid_3rd = model(images, targets)
        if cfg.MODEL.LOSS.USE_SOFTMAX:
            softmax_loss_2nd = cfg.SOLVER.LW_RCNN_SOFTMAX_2ND * softmax_criterion_s2(feats_reid_2nd, targets_reid_2nd)
            softmax_loss_3rd = cfg.SOLVER.LW_RCNN_SOFTMAX_3RD * softmax_criterion_s3(feats_reid_3rd, targets_reid_3rd)
            loss_dict.update(loss_box_softmax_2nd=softmax_loss_2nd)
            loss_dict.update(loss_box_softmax_3rd=softmax_loss_3rd)
        losses = sum(loss for loss in loss_dict.values())

        # reduce losses over all GPUs for logging purposes
        loss_dict_reduced = reduce_dict(loss_dict)
        losses_reduced = sum(loss for loss in loss_dict_reduced.values())
        loss_value = losses_reduced.item()

        if not math.isfinite(loss_value):
            write_text("Loss is {}, stopping training".format(loss_value), fpath=os.path.join(outsys_dir, 'os.txt'))
            write_text(loss_dict_reduced, fpath=os.path.join(outsys_dir, 'os.txt'))
            sys.exit(1)

        optimizer.zero_grad()
        losses.backward()
        if cfg.SOLVER.CLIP_GRADIENTS > 0:
            clip_grad_norm_(model.parameters(), cfg.SOLVER.CLIP_GRADIENTS)
        optimizer.step()

        if epoch == 0:
            warmup_scheduler.step()

        metric_logger.update(loss=loss_value, **loss_dict_reduced)
        metric_logger.update(lr=optimizer.param_groups[0]["lr"])
        if tfboard:
            iter = epoch * len(data_loader) + i
            for k, v in loss_dict_reduced.items():
                tfboard.add_scalars("train", {k: v}, iter)


@torch.no_grad()
def evaluate_performance(
    model, gallery_loader, query_loader, device, use_gt=False, use_cache=False, use_cbgm=False, outsys_dir = None):
    """
    Args:
        use_gt (bool, optional): Whether to use GT as detection results to verify the upper
                                bound of person search performance. Defaults to False.
        use_cache (bool, optional): Whether to use the cached features. Defaults to False.
        use_cbgm (bool, optional): Whether to use Context Bipartite Graph Matching algorithm.
                                Defaults to False.
    """
    model.eval()
    if use_cache:
        eval_cache = torch.load("data/eval_cache/eval_cache.pth")
        gallery_dets = eval_cache["gallery_dets"]
        gallery_feats = eval_cache["gallery_feats"]
        query_dets = eval_cache["query_dets"]
        query_feats = eval_cache["query_feats"]
        query_box_feats = eval_cache["query_box_feats"]
    else:
        gallery_dets, gallery_feats = [], []
        for images, targets in tqdm(gallery_loader, ncols=0):   #images:[3,450,800] targets:{img_name, boxees, labels}
            images, targets = to_device(images, targets, device)
            if not use_gt:
                outputs = model(images)    ###推理阶段  outputs是一个字典 {‘boxes’, 'labels', 'scores', 'embeddings'}
            else:
                boxes = targets[0]["boxes"]
                n_boxes = boxes.size(0)
                embeddings = model(images, targets)
                outputs = [
                    {
                        "boxes": boxes,
                        "embeddings": torch.cat(embeddings),
                        "labels": torch.ones(n_boxes).to(device),
                        "scores": torch.ones(n_boxes).to(device),
                    }
                ]

            for output in outputs:
                box_w_scores = torch.cat([output["boxes"], output["scores"].unsqueeze(1)], dim=1)   ###推理阶段  将boxes与scores合并，(4,5)
                gallery_dets.append(box_w_scores.cpu().numpy())    ###推理阶段(4,5)
                gallery_feats.append(output["embeddings"].cpu().numpy())  ###推理阶段

        # regarding query image as gallery to detect all people
        # i.e. query person + surrounding people (context information)
        query_dets, query_feats = [], []
        for images, targets in tqdm(query_loader, ncols=0):
            images, targets = to_device(images, targets, device)
            # targets will be modified in the model, so deepcopy it
            outputs = model(images, deepcopy(targets), query_img_as_gallery=True)    ##里面包含了gt的 boxes, scores, labels, embeddings

            # consistency check
            gt_box = targets[0]["boxes"].squeeze()
            assert (
                gt_box - outputs[0]["boxes"][0]
            ).sum() <= 0.001, "GT box must be the first one in the detected boxes of query image"

            for output in outputs:
                box_w_scores = torch.cat([output["boxes"], output["scores"].unsqueeze(1)], dim=1)
                query_dets.append(box_w_scores.cpu().numpy())
                query_feats.append(output["embeddings"].cpu().numpy())

        # extract the features of query boxes
        query_box_feats = []
        for images, targets in tqdm(query_loader, ncols=0):
            images, targets = to_device(images, targets, device)
            embeddings = model(images, targets)
            assert len(embeddings) == 1, "batch size in test phase should be 1"
            query_box_feats.append(embeddings[0].cpu().numpy())

        mkdir("data/eval_cache")
        save_dict = {
            "gallery_dets": gallery_dets,
            "gallery_feats": gallery_feats,
            "query_dets": query_dets,
            "query_feats": query_feats,
            "query_box_feats": query_box_feats,
        }
        torch.save(save_dict, "data/eval_cache/eval_cache.pth")
    if outsys_dir is not None:
        eval_detection(gallery_loader.dataset, gallery_dets, det_thresh=0.01, outsys_dir=outsys_dir)
        eval_search_func = (
            eval_search_cuhk if gallery_loader.dataset.name == "CUHK-SYSU" else eval_search_prw
        )
        eval_search_func(
            gallery_loader.dataset,
            query_loader.dataset,
            gallery_dets,
            gallery_feats,
            query_box_feats,
            query_dets,
            query_feats,
            cbgm=use_cbgm,
            outsys_dir=outsys_dir
        )