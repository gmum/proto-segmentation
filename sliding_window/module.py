"""
Pytorch Lightning Module for training prototype segmentation model on Cityscapes and SUN datasets
"""
import os
from typing import Dict, Optional

import gin
import torch
from pytorch_lightning import LightningModule
from torch.optim.lr_scheduler import ReduceLROnPlateau

from helpers import list_of_distances
from model import PPNet
from save import save_model_w_condition
from settings import log
from train_and_test import warm_only, joint, last_only

ReduceLROnPlateau = gin.external_configurable(ReduceLROnPlateau)


def get_lr(optimizer):
    for param_group in optimizer.param_groups:
        return param_group['lr']


def reset_metrics() -> Dict:
    return {
        'n_examples': 0,
        'n_correct': 0,
        'n_batches': 0,
        'cross_entropy': 0,
        'cluster_cost': 0,
        'loss': 0,
        'separation_cost': 0,
        'avg_separation_cost': 0
    }


def update_lr_warmup(optimizer, current_step, warmup_batches):
    if warmup_batches > 0:
        if current_step < warmup_batches:
            lr_scale = min(1., float(current_step + 1) / warmup_batches)
            for pg in optimizer.param_groups:
                pg['lr'] = lr_scale * optimizer.defaults['lr']


# noinspection PyAbstractClass
@gin.configurable(denylist=['model_dir', 'model_image_size', 'ppnet', 'last_layer_only'])
class SlidingWindowModule(LightningModule):
    def __init__(
            self,
            model_dir: str,
            model_image_size: int,
            ppnet: PPNet,
            last_layer_only: bool,
            class_specific: bool = gin.REQUIRED,
            num_warm_epochs: int = gin.REQUIRED,
            loss_weight_crs_ent: float = gin.REQUIRED,
            loss_weight_clst: float = gin.REQUIRED,
            loss_weight_sep: float = gin.REQUIRED,
            loss_weight_l1: float = gin.REQUIRED,
            joint_optimizer_lr_features: float = gin.REQUIRED,
            joint_optimizer_lr_add_on_layers: float = gin.REQUIRED,
            joint_optimizer_lr_prototype_vectors: float = gin.REQUIRED,
            joint_optimizer_weight_decay: float = gin.REQUIRED,
            warm_optimizer_lr_add_on_layers: float = gin.REQUIRED,
            warm_optimizer_lr_prototype_vectors: float = gin.REQUIRED,
            warm_optimizer_weight_decay: float = gin.REQUIRED,
            last_layer_optimizer_lr: float = gin.REQUIRED,
            warmup_batches: int = gin.REQUIRED,
            gradient_clipping: Optional[float] = gin.REQUIRED
    ):
        super().__init__()
        self.model_dir = model_dir
        self.prototypes_dir = os.path.join(model_dir, 'prototypes')
        self.checkpoints_dir = os.path.join(model_dir, 'checkpoints')
        self.model_image_size = model_image_size
        self.ppnet = ppnet
        self.last_layer_only = last_layer_only
        self.class_specific = class_specific
        self.num_warm_epochs = num_warm_epochs
        self.loss_weight_crs_ent = loss_weight_crs_ent
        self.loss_weight_clst = loss_weight_clst
        self.loss_weight_sep = loss_weight_sep
        self.loss_weight_l1 = loss_weight_l1
        self.joint_optimizer_lr_features = joint_optimizer_lr_features
        self.joint_optimizer_lr_add_on_layers = joint_optimizer_lr_add_on_layers
        self.joint_optimizer_lr_prototype_vectors = joint_optimizer_lr_prototype_vectors
        self.joint_optimizer_weight_decay = joint_optimizer_weight_decay
        self.warm_optimizer_lr_add_on_layers = warm_optimizer_lr_add_on_layers
        self.warm_optimizer_lr_prototype_vectors = warm_optimizer_lr_prototype_vectors
        self.warm_optimizer_weight_decay = warm_optimizer_weight_decay
        self.last_layer_optimizer_lr = last_layer_optimizer_lr
        self.warmup_batches = warmup_batches
        self.gradient_clipping = gradient_clipping

        os.makedirs(self.prototypes_dir, exist_ok=True)
        os.makedirs(self.checkpoints_dir, exist_ok=True)

        # initialize variables for computing metrics
        self.metrics = {}
        for split_key in ['train', 'val', 'test', 'train_last_layer']:
            self.metrics[split_key] = reset_metrics()

        # initialize configure_optimizers()
        self.lr_scheduler = None

        # we use optimizers manually
        self.automatic_optimization = False

        self.best_acc = 0.0

    def forward(self, x):
        return self.ppnet(x)

    def _step(self, split_key: str, batch):
        metrics = self.metrics[split_key]

        image, target = batch
        image = image.to(self.device)
        target = target.to(self.device)

        output, min_distances = self.ppnet(image)

        # noinspection PyUnresolvedReferences
        cross_entropy = torch.nn.functional.cross_entropy(output, target)
        separation_cost = 0

        if self.class_specific:
            max_dist = (self.ppnet.prototype_shape[1]
                        * self.ppnet.prototype_shape[2]
                        * self.ppnet.prototype_shape[3])

            # prototypes_of_correct_class is a tensor of shape batch_size * num_prototypes
            # calculate cluster cost
            prototypes_of_correct_class = torch.t(torch.index_select(
                self.ppnet.prototype_class_identity.to(self.device),
                dim=-1,
                index=target
            )).to(self.device)

            inverted_distances, _ = torch.max((max_dist - min_distances) * prototypes_of_correct_class, dim=1)

            # ignore 'void' class (0) in calculating cluster loss
            inverted_distances[target == 0] = max_dist
            cluster_cost = torch.mean(max_dist - inverted_distances)

            # calculate separation cost
            prototypes_of_wrong_class = 1 - prototypes_of_correct_class
            inverted_distances_to_nontarget_prototypes, _ = \
                torch.max((max_dist - min_distances) * prototypes_of_wrong_class, dim=1)

            # optionally: ignore separation loss for 'void' class
            # (we might not care if prototypes are "similar" to void class - we do not use prototypes for it)
            # inverted_distances_to_nontarget_prototypes[target == 0] = max_dist

            separation_cost = torch.mean(max_dist - inverted_distances_to_nontarget_prototypes)

            # calculate avg cluster cost
            avg_separation_cost = \
                torch.sum(min_distances * prototypes_of_wrong_class, dim=1) / torch.sum(
                    prototypes_of_wrong_class, dim=1)
            avg_separation_cost = torch.mean(avg_separation_cost)

            l1_mask = 1 - torch.t(self.ppnet.prototype_class_identity).to(self.device)
            l1 = (self.ppnet.last_layer.weight * l1_mask).norm(p=1)

            metrics['separation_cost'] += separation_cost.item()
            metrics['avg_separation_cost'] += avg_separation_cost.item()

        else:
            min_distance, _ = torch.min(min_distances, dim=1)
            cluster_cost = torch.mean(min_distance)
            l1 = self.ppnet.last_layer.weight.norm(p=1)

        # evaluation statistics
        _, predicted = torch.max(output.data, 1)
        metrics['n_examples'] += target.size(0)
        metrics['n_correct'] += (predicted == target).sum().item()

        metrics['n_batches'] += 1
        metrics['cross_entropy'] += cross_entropy.item()
        metrics['cluster_cost'] += cluster_cost.item()

        if self.class_specific:
            loss = (self.loss_weight_crs_ent * cross_entropy
                    + self.loss_weight_clst * cluster_cost
                    + self.loss_weight_sep * separation_cost
                    + self.loss_weight_l1 * l1)
        else:
            loss = (self.loss_weight_crs_ent * cross_entropy
                    + self.loss_weight_clst * cluster_cost
                    + self.loss_weight_sep * l1)
        loss_value = loss.item()
        metrics['loss'] += loss_value

        if split_key == 'train':
            warm_optim, main_optim = self.optimizers()

            if self.last_layer_only:
                optimizer = main_optim
            else:
                if self.current_epoch < self.num_warm_epochs:
                    optimizer = warm_optim
                else:
                    optimizer = main_optim

            update_lr_warmup(optimizer, self.trainer.global_step, self.warmup_batches)

            optimizer.zero_grad()
            self.manual_backward(loss)

            if self.gradient_clipping is not None:
                torch.nn.utils.clip_grad_norm_(self.ppnet.parameters(), self.gradient_clipping)

            optimizer.step()
            self.log('train_loss_step', loss_value, on_step=True, prog_bar=True)

            lr = get_lr(optimizer)
            self.log('lr', lr, on_step=True)

    def training_step(self, batch, batch_idx):
        return self._step('train', batch)

    def validation_step(self, batch, batch_idx):
        return self._step('val', batch)

    def test_step(self, batch, batch_idx):
        return self._step('test', batch)

    def on_train_epoch_start(self):
        if self.last_layer_only:
            last_only(model=self.ppnet, log=log)
        else:
            if self.current_epoch == 0:
                log('WARM-UP START.')

            if self.current_epoch < self.num_warm_epochs:
                warm_only(model=self.ppnet, log=log)
            else:
                joint(model=self.ppnet, log=log)
                if self.current_epoch == self.num_warm_epochs:
                    log('WARM-UP END.')

        # reset metrics
        for split_key in self.metrics.keys():
            self.metrics[split_key] = reset_metrics()

    def on_validation_epoch_end(self):
        val_acc = self.metrics['val']['n_correct'] / self.metrics['val']['n_examples']
        val_loss = self.metrics['val']['loss'] / self.metrics['val']['n_batches']

        if self.last_layer_only:
            self.log('training_stage', 2.0)
            stage_key = 'push'
            self.lr_scheduler.step(val_acc)
        else:
            if self.current_epoch < self.num_warm_epochs:
                # noinspection PyUnresolvedReferences
                self.log('training_stage', 0.0)
                stage_key = 'warmup'
            else:
                # noinspection PyUnresolvedReferences
                self.log('training_stage', 1.0)
                stage_key = 'nopush'
                self.lr_scheduler.step(val_acc)

        if val_acc > self.best_acc:
            log(f'Saving best model, accuracy: ' + str(val_acc) + ', loss: ' + str(val_loss))
            self.best_acc = val_acc
            save_model_w_condition(
                model=self.ppnet,
                model_dir=self.checkpoints_dir,
                model_name=f'{stage_key}_best',
                accu=val_acc,
                target_accu=0.0,
                log=log
            )
        save_model_w_condition(
            model=self.ppnet,
            model_dir=self.checkpoints_dir,
            model_name=f'{stage_key}_last',
            accu=val_acc,
            target_accu=0.0,
            log=log
        )

    def _epoch_end(self, split_key: str):
        metrics = self.metrics[split_key]
        n_batches = metrics['n_batches']

        for key in ['loss', 'cross_entropy', 'cluster_cost', 'separation_cost', 'avg_separation_cost']:
            self.log(f'{split_key}/{key}', metrics[key] / n_batches)

        self.log(f'{split_key}/accuracy', metrics['n_correct'] / metrics['n_examples'])
        self.log('l1', self.ppnet.last_layer.weight.norm(p=1).item())

        p = self.ppnet.prototype_vectors.view(self.ppnet.num_prototypes, -1).cpu()
        with torch.no_grad():
            p_avg_pair_dist = torch.mean(list_of_distances(p, p))
        self.log('p dist pair', p_avg_pair_dist.item())

    def training_epoch_end(self, step_outputs):
        return self._epoch_end('train')

    def validation_epoch_end(self, step_outputs):
        return self._epoch_end('val')

    def test_epoch_end(self, step_outputs):
        return self._epoch_end('test')

    def configure_optimizers(self):
        warm_optimizer_specs = \
            [
                {
                    'params': self.ppnet.add_on_layers.parameters(),
                    'lr': self.warm_optimizer_lr_add_on_layers,
                    'weight_decay': self.warm_optimizer_weight_decay
                },
                {
                    'params': self.ppnet.prototype_vectors,
                    'lr': self.warm_optimizer_lr_prototype_vectors
                }
            ]
        warm_optimizer = torch.optim.Adam(warm_optimizer_specs)

        if self.last_layer_only:
            main_optimizer_specs = [
                {
                    'params': self.ppnet.last_layer.parameters(),
                    'lr': self.last_layer_optimizer_lr
                }
            ]
        else:
            main_optimizer_specs = \
                [
                    {
                        'params': self.ppnet.features.parameters(),
                        'lr': self.joint_optimizer_lr_features,
                        'weight_decay': self.joint_optimizer_weight_decay
                    },
                    # bias are now also being regularized
                    {
                        'params': self.ppnet.add_on_layers.parameters(),
                        'lr': self.joint_optimizer_lr_add_on_layers,
                        'weight_decay': self.joint_optimizer_weight_decay
                    },
                    {
                        'params': self.ppnet.prototype_vectors,
                        'lr': self.joint_optimizer_lr_prototype_vectors
                    },
                ]
        main_optimizer = torch.optim.Adam(main_optimizer_specs)
        self.lr_scheduler = ReduceLROnPlateau(main_optimizer)

        return warm_optimizer, main_optimizer