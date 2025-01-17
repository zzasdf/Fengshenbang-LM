import numpy
from pytorch_lightning import LightningDataModule
from typing import Optional
from torch.utils.data import DataLoader, DistributedSampler


def get_consume_samples(data_model: LightningDataModule) -> int:
    if hasattr(data_model.trainer.lightning_module, 'consumed_samples'):
        consumed_samples = data_model.trainer.lightning_module.consumed_samples
        print('get consumed samples from model: {}'.format(consumed_samples))
    else:
        world_size = data_model.trainer.world_size
        consumed_samples = max(0, data_model.trainer.global_step - 1) * \
            data_model.hparams.train_batchsize * world_size * data_model.trainer.accumulate_grad_batches
        print('calculate consumed samples: {}'.format(consumed_samples))
    return consumed_samples


class UniversalDataModule(LightningDataModule):
    @ staticmethod
    def add_data_specific_args(parent_args):
        parser = parent_args.add_argument_group('Universal DataModule')
        parser.add_argument('--num_workers', default=8, type=int)
        parser.add_argument('--dataloader_workers', default=2, type=int)
        parser.add_argument('--train_batchsize', default=32, type=int)
        parser.add_argument('--val_batchsize', default=32, type=int)
        parser.add_argument('--test_batchsize', default=32, type=int)
        parser.add_argument('--datasets_name', type=str, default=None)
        parser.add_argument('--train_datasets_field', type=str, default='train')
        parser.add_argument('--val_datasets_field', type=str, default='validation')
        parser.add_argument('--test_datasets_field', type=str, default='test')
        parser.add_argument('--sampler_type', type=str,
                            choices=['single',
                                     'random',
                                     'fairseq'],
                            default='random')
        return parent_args

    def __init__(
        self,
        tokenizer,
        collate_fn,
        args,
        datasets=None,
        **kwargs,
    ):
        super().__init__()
        # 如果不传入datasets的名字，则可以在对象外部替换内部的datasets为模型需要的
        if datasets is None:
            from fengshen.data.fs_datasets import load_dataset
            print('---------begin to load datasets {}'.format(args.datasets_name), flush=True)
            self.datasets = load_dataset(
                args.datasets_name, num_proc=args.num_workers)
            print('---------ending load datasets {}'.format(args.datasets_name))
        else:
            self.datasets = datasets
        self.tokenizer = tokenizer
        self.collate_fn = collate_fn
        self.save_hyperparameters(args)

    def get_custom_sampler(self, ds):
        from .universal_sampler import PretrainingRandomSampler
        from .universal_sampler import PretrainingSampler
        from .universal_sampler import BatchRandomSampler
        world_size = self.trainer.world_size
        consumed_samples = get_consume_samples(self)
        # use the user default sampler
        if self.hparams.sampler_type == 'random':
            return PretrainingRandomSampler(
                total_samples=len(ds),
                # consumed_samples cal by global steps
                consumed_samples=consumed_samples,
                micro_batch_size=self.hparams.train_batchsize,
                data_parallel_rank=self.trainer.global_rank,
                data_parallel_size=world_size,
                epoch=self.trainer.current_epoch,
            )
        elif self.hparams.sampler_type == 'single':
            return PretrainingSampler(
                total_samples=len(ds),
                # consumed_samples cal by global steps
                consumed_samples=consumed_samples,
                micro_batch_size=self.hparams.train_batchsize,
                data_parallel_rank=self.trainer.global_rank,
                data_parallel_size=world_size,
            )
        elif self.hparams.sampler_type == 'fairseq':
            # 暂时引用fairseq的实现，等对其效果以后再自己实现我们的sampler
            from fairseq.data.fairseq_dataset import FairseqDataset
            from fairseq.data import data_utils
            from fairseq.data.iterators import ShardedIterator
            import numpy as np
            assert hasattr(
                ds, 'batch_by_size'), "sampler type fairseq need attr batch_by_size not found"
            # 判断一下
            fairseq_paras = ['max_tokens', 'required_batch_size_multiple']
            for p in fairseq_paras:
                assert hasattr(self.hparams, p), f"--{p} not found in args"
            # get indices ordered by example size
            with data_utils.numpy_seed(42):
                indices = ds.ordered_indices()
            batches = ds.batch_by_size(
                indices,
                max_tokens=self.hparams.max_tokens,
                max_sentences=self.hparams.train_batchsize,
                required_batch_size_multiple=self.hparams.required_batch_size_multiple,
            )
            # 需要取一个当前的batch数，从ckpt重启时需要恢复batches的状态，去掉已经消费的batches
            # 当前过的总batch数 除以 数据集长度取余
            offset = (self.trainer.fit_loop.epoch_loop._batches_that_stepped *
                      self.trainer.world_size) % len(batches)

            sampler = BatchRandomSampler(batches, offset, 
                                         data_parallel_rank=self.trainer.global_rank,
                                         data_parallel_size=world_size,
                                         epoch=self.trainer.current_epoch,)
            return sampler
        else:
            raise Exception('Unknown sampler type: {}'.format(self.hparams.sampler_type))

    def setup(self, stage: Optional[str] = None) -> None:
        return

    def train_dataloader(self):
        ds = self.datasets[self.hparams.train_datasets_field]

        collate_fn = self.collate_fn
        if collate_fn is None and hasattr(ds, 'collater'):
            collate_fn = ds.collater

        if self.hparams.replace_sampler_ddp is False:
            return DataLoader(
                ds,
                batch_sampler=self.get_custom_sampler(ds),
                num_workers=self.hparams.dataloader_workers,
                collate_fn=collate_fn,
                pin_memory=True,
            )
        return DataLoader(
            ds,
            batch_size=self.hparams.train_batchsize,
            num_workers=self.hparams.num_workers,
            collate_fn=collate_fn,
            pin_memory=True,
        )

    def val_dataloader(self):
        ds = self.datasets[self.hparams.val_datasets_field]
        collate_fn = self.collate_fn
        if collate_fn is None and hasattr(ds, 'collater'):
            collate_fn = ds.collater

        return DataLoader(
            ds,
            batch_size=self.hparams.val_batchsize,
            shuffle=False,
            num_workers=self.hparams.num_workers,
            collate_fn=collate_fn,
            sampler=DistributedSampler(
                ds, shuffle=False),
            pin_memory=True,
        )

    def test_dataloader(self):
        ds = self.datasets[self.hparams.test_datasets_field],

        collate_fn = self.collate_fn
        if collate_fn is None and hasattr(ds, 'collater'):
            collate_fn = ds.collater

        return DataLoader(
            ds,
            batch_size=self.hparams.test_batchsize,
            shuffle=False,
            num_workers=self.hparams.dataloader_workers,
            collate_fn=collate_fn,
            sampler=DistributedSampler(
                ds, shuffle=False),
            pin_memory=True,
        )
