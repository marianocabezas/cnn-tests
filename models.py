import time
from copy import deepcopy
import torch
from torch import nn
import torch.nn.functional as F
import numpy as np
from utils import time_to_string


class BaseModel(nn.Module):
    def __init__(self):
        super().__init__()
        # Init values
        self.optimizer_alg = None
        self.epoch = 0
        self.t_train = 0
        self.t_val = 0
        self.dropout = 0
        self.ann_rate = 0
        self.best_state = None
        self.best_opt = None
        self.train_functions = [
            {'name': 'train', 'weight': 1, 'f': None},
        ]
        self.val_functions = [
            {'name': 'val', 'weight': 1, 'f': None},
        ]
        self.acc_functions = {}
        self.acc = None

    def forward(self, inputs):
        return None

    def mini_batch_loop(
            self, training, train=True
    ):
        losses = list()
        mid_losses = list()
        n_batches = len(training)
        for batch_i, (x, y) in enumerate(training):
            # We train the model and check the loss
            if self.training:
                self.optimizer_alg.zero_grad()

            torch.cuda.synchronize()
            pred_labels = self(x.to(self.device))

            # Training losses
            if self.training:
                batch_loss = torch.sum(
                    [
                        l_f['weight'] * l_f['f'](pred_labels, y)
                        for l_f in self.train_functions
                    ]
                )
                batch_loss.backward()
                self.optimizer_alg.step()
            else:
                # roi_value = torch.mean(batch_loss_r).tolist()
                # tumor_value = torch.mean(batch_loss_t).tolist()
                # loss_value = roi_value + tumor_value
                batch_losses = [
                    l_f['weight'] * l_f['f'](pred_labels, y)
                    for l_f in self.val_functions
                ]
                batch_loss = torch.sum(batch_losses)
                mid_losses.append([l.tolist() for l in batch_losses])

            torch.cuda.synchronize()
            torch.cuda.empty_cache()

            loss_value = batch_loss.tolist()
            losses.append(loss_value)

            # Curriculum dropout
            # (1 - rho) * exp(- gamma * t) + rho, gamma > 0

            self.print_progress(
                batch_i, n_batches, loss_value, np.mean(losses), train
            )

        if train:
            return np.mean(losses)
        else:
            return np.mean(losses), np.mean(zip(*mid_losses), axis=1)

    def fit(
            self,
            train_loader,
            val_loader,
            epochs=50,
            patience=5,
            verbose=True
    ):
        # Init
        self.train()
        best_e = 0
        no_improv_e = 0
        best_loss_tr = np.inf
        best_loss_val = np.inf
        l_names = ['train', ' val '] + [
            '{:^8s}'.format(l_f['name']) for l_f in self.val_functions[1:]
        ]
        acc_names = [
            '{:^8s}'.format(a_f['name']) for a_f in self.acc_functions
        ]
        best_losses = (len(l_names) - 2) * [np.inf]
        best_acc = len(acc_names) * [-np.inf]
        self.best_state = deepcopy(self.state_dict())
        self.best_opt = deepcopy(self.optimizer_alg.state_dict())
        t_start = time.time()

        for self.epoch in range(epochs):
            # Main epoch loop
            self.t_train = time.time()
            loss_tr = self.mini_batch_loop(train_loader)
            improvement_tr = loss_tr < best_loss_tr
            if improvement_tr:
                best_loss_tr = loss_tr
                tr_loss_s = '\033[32m{:7.4f}\033[0m'.format(loss_tr)
            else:
                tr_loss_s = '{:7.4f}'.format(loss_tr)

            with torch.no_grad():
                self.t_val = time.time()
                loss_val, mid_losses, acc = self.mini_batch_loop(
                    val_loader, False
                )

            # Mid losses check
            losses_s = [
                '\033[36m{:8.4f}\033[0m'.format(l) if pl > l
                else '{:}'.format(l) for pl, l in zip(
                    best_losses, mid_losses
                )
            ]
            best_losses = [
                l if pl > l else pl for pl, l in zip(
                    best_losses, mid_losses
                )
            ]
            # Acc check
            acc_s = [
                '\033[36m{:8.4f}\033[0m'.format(a) if pa < a
                else '{:}'.format(a) for pa, a  in zip(
                    best_acc, acc
                )
            ]
            best_acc = [
                a if pa < a else pa for pa, a in zip(
                    best_acc, acc
                )
            ]

            # Patience check
            improvement_val = loss_val < best_loss_val
            loss_s = '{:7.5f}'.format(loss_val)
            if improvement_val:
                best_loss_val = loss_val
                epoch_s = '\033[32mEpoch {:03d}\033[0m'.format(self.epoch)
                loss_s = '\033[32m{:}\033[0m'.format(loss_s)
                best_e = self.epoch
                self.best_state = deepcopy(self.state_dict())
                self.best_opt = deepcopy(self.optimizer_alg.state_dict())
                no_improv_e = 0
            else:
                epoch_s = 'Epoch {:03d}'.format(self.epoch)
                no_improv_e += 1

            t_out = time.time() - self.t_train
            t_s = time_to_string(t_out)

            drop_s = '{:5.3f}'.format(self.dropout)
            if self.final_dropout <= self.dropout:
                self.dropout = max(
                    self.final_dropout, self.dropout - self.ann_rate
                )

            if verbose:
                print('\033[K', end='')
                whites = ' '.join([''] * 12)
                if self.epoch == 0:
                    l_bars = '--|--'.join(
                        ['-' * 5] * 2 +
                        ['-' * 6] * (len(l_names[2:]) + len(acc_names)) +
                        ['-' * 3]
                    )
                    l_hdr = '  |  '.join(l_names + acc_names + ['p_drp'])
                    print('{:}Epoch num |  {:}  |'.format(whites, l_hdr))
                    print('{:}----------|--{:}--|'.format(whites, l_bars))
                final_s = whites + ' | '.join(
                    [epoch_s, tr_loss_s, loss_s] +
                    losses_s + acc_s + [drop_s, t_s]
                )
                print(final_s)

            if no_improv_e == int(patience / (1 - self.dropout)):
                break

        self.epoch = best_e
        self.load_state_dict(self.best_state)
        t_end = time.time() - t_start
        t_end_s = time_to_string(t_end)
        if verbose:
            print(
                    'Training finished in %d epochs ({:}) '
                    'with minimum loss = {:f} (epoch {:d})'.format(
                        self.epoch + 1, t_end_s, best_loss_val, best_e
                    )
            )

    def print_progress(self, batch_i, n_batches, b_loss, mean_loss, train=True):
        init_c = '\033[0m' if train else '\033[38;5;238m'
        whites = ' '.join([''] * 12)
        percent = 20 * (batch_i + 1) / n_batches
        progress_s = ''.join(['-'] * percent)
        remainder_s = ''.join([' '] * (20 - percent))
        loss_name = 'train_loss' if train else 'val_loss'

        if train:
            t_out = time.time() - self.t_train
        else:
            t_out = time.time() - self.t_val
        time_s = time_to_string(t_out)

        t_eta = (t_out / (batch_i + 1)) * (n_batches - (batch_i + 1))
        eta_s = time_to_string(t_eta)
        epoch_hdr = '{:}Epoch {:03} ({:03d}/{:03d}) [{:}>{:}] '
        loss_s = '{:} {:f} ({:f}) {:} / ETA {:}'
        batch_s = (epoch_hdr + loss_s).format(
            init_c + whites, self.epoch, batch_i + 1, n_batches,
            progress_s, remainder_s,
            loss_name, b_loss, mean_loss, time_s, eta_s + '\033[0m'
        )
        print('\033[K', end='', flush=True)
        print(batch_s, end='\r', flush=True)


class Autoencoder(nn.Module):
    def __init__(
            self,
            conv_filters,
            device=torch.device(
                "cuda:0" if torch.cuda.is_available() else "cpu"
            ),
            n_inputs=1,
            pooling=False,
            dropout=0,
    ):
        super().__init__()
        # Init
        self.pooling = pooling
        self.device = device
        self.dropout = dropout
        # Down path of the unet
        self.down = nn.ModuleList([
            nn.Sequential(
                nn.Conv3d(
                    f_in, f_out, 3,
                    padding=1,
                ),
                nn.ReLU()
            ) for f_in, f_out in zip(
                [n_inputs] + conv_filters[:-2], conv_filters[:-1]
            )
        ])

        self.u = nn.Sequential(
            nn.Conv3d(
                conv_filters[-2], conv_filters[-1], 3,
                padding=1
            ),
            nn.ReLU()
        )

        # Up path of the unet
        down_out = conv_filters[-2::-1]
        up_out = conv_filters[:0:-1]
        deconv_in = map(sum, zip(down_out, up_out))
        self.up = nn.ModuleList([
            nn.Sequential(
                nn.ConvTranspose3d(
                    f_in, f_out, 3,
                    padding=1
                ),
                nn.ReLU()
            ) for f_in, f_out in zip(
                deconv_in, down_out
            )
        ])

    def forward(self, input_s):
        down_inputs = []
        for c in self.down:
            c.to(self.device)
            input_s = F.dropout3d(
                c(input_s), self.dropout, self.training
            )
            down_inputs.append(input_s)
            if self.pooling:
                input_s = F.max_pool3d(input_s, 2)

        self.u.to(self.device)
        input_s = self.u(input_s)

        for d, i in zip(self.up, down_inputs[::-1]):
            d.to(self.device)
            if self.pooling:
                input_s = F.dropout3d(
                    d(
                        torch.cat(
                            (F.interpolate(input_s, size=i.size()[2:]), i),
                            dim=1
                        )
                    ),
                    self.dropout,
                    self.training
                )
            else:
                input_s = d(torch.cat((input_s, i), dim=1))

        return input_s
