"""Variational autoencoder models with GAN loss implemented in PyTorch."""

import numpy as np
from sklearn.metrics import r2_score
import torch
from torch import nn

import behavenet.fitting.losses as losses
from behavenet.models.aes import AE, ConvAEDecoder, ConvAEEncoder
from behavenet.models.base import BaseModule, BaseModel
from behavenet.models.vaes import reparameterize, VAE

# to ignore imports for sphix-autoapidoc
__all__ = ['VAEGAN']


class VAEGAN(VAE):
    """Variational autoencoder with GAN loss class.

    This class constructs convolutional variational autoencoders. The convolutional autoencoder
    architecture is defined by various keys in the dict that serves as the constructor input. See
    the :mod:`behavenet.fitting.ae_model_architecture_generator` module to see examples for how
    this is done.

    The VAE class can also be used to fit β-VAE models (see https://arxiv.org/pdf/1804.03599.pdf)
    by changing the value of the `vae.beta` parameter in the `ae_model.json` file; a value of 1
    corresponds to a standard VAE; a value >1 will upweight the KL divergence term which, in some
    cases, can lead to disentangling of the latent representation.
    """

    def __init__(self, hparams):
        """

        Parameters
        ----------
        hparams : :obj:`dict`
            - 'model_type' (:obj:`int`): 'conv'
            - 'model_class' (:obj:`str`): 'vae'
            - 'y_pixels' (:obj:`int`)
            - 'x_pixels' (:obj:`int`)
            - 'n_input_channels' (:obj:`int`)
            - 'n_ae_latents' (:obj:`int`)
            - 'fit_sess_io_layers; (:obj:`bool`): fit session-specific input/output layers
            - 'vae.beta' (:obj:`float`)
            - 'vae.beta_anneal_epochs' (:obj:`int`)
            - 'ae_encoding_x_dim' (:obj:`list`)
            - 'ae_encoding_y_dim' (:obj:`list`)
            - 'ae_encoding_n_channels' (:obj:`list`)
            - 'ae_encoding_kernel_size' (:obj:`list`)
            - 'ae_encoding_stride_size' (:obj:`list`)
            - 'ae_encoding_x_padding' (:obj:`list`)
            - 'ae_encoding_y_padding' (:obj:`list`)
            - 'ae_encoding_layer_type' (:obj:`list`)
            - 'ae_decoding_x_dim' (:obj:`list`)
            - 'ae_decoding_y_dim' (:obj:`list`)
            - 'ae_decoding_n_channels' (:obj:`list`)
            - 'ae_decoding_kernel_size' (:obj:`list`)
            - 'ae_decoding_stride_size' (:obj:`list`)
            - 'ae_decoding_x_padding' (:obj:`list`)
            - 'ae_decoding_y_padding' (:obj:`list`)
            - 'ae_decoding_layer_type' (:obj:`list`)
            - 'ae_decoding_starting_dim' (:obj:`list`)
            - 'ae_decoding_last_FF_layer' (:obj:`bool`)

        """
        if hparams['model_type'] == 'linear':
            raise NotImplementedError
        hparams['variational'] = True
        super().__init__(hparams)

        # set up kl annealing
        anneal_epochs = self.hparams.get('vae.beta_anneal_epochs', 0)
        self.curr_epoch = 0  # must be modified by training script
        if anneal_epochs > 0:
            self.beta_vals = np.append(
                np.linspace(0, hparams['vae.beta'], anneal_epochs),
                np.ones(hparams['max_n_epochs'] + 1))  # sloppy addition to fully cover rest
        else:
            self.beta_vals = hparams['vae.beta'] * np.ones(hparams['max_n_epochs'] + 1)

    def forward(self, x, dataset=None, use_mean=False, **kwargs):
        """Process input data.

        Parameters
        ----------
        x : :obj:`torch.Tensor` object
            input data
        dataset : :obj:`int`
            used with session-specific io layers
        use_mean : :obj:`bool`
            True to skip sampling step

        Returns
        -------
        :obj:`tuple`
            - x_hat (:obj:`torch.Tensor`): output of shape (n_frames, n_channels, y_pix, x_pix)
            - z (:obj:`torch.Tensor`): sampled latent variable of shape (n_frames, n_latents)
            - mu (:obj:`torch.Tensor`): mean paramter of shape (n_frames, n_latents)
            - logvar (:obj:`torch.Tensor`): logvar paramter of shape (n_frames, n_latents)

        """
        mu, logvar, pool_idx, outsize = self.encoding(x, dataset=dataset)
        if use_mean:
            z = mu
        else:
            z = reparameterize(mu, logvar)
        x_hat = self.decoding(z, pool_idx, outsize, dataset=dataset)
        return x_hat, z, mu, logvar

    def loss(self, data, dataset=0, accumulate_grad=True, chunk_size=200):
        """Calculate ELBO loss for VAE.

        The batch is split into chunks if larger than a hard-coded `chunk_size` to keep memory
        requirements low; gradients are accumulated across all chunks before a gradient step is
        taken.

        Parameters
        ----------
        data : :obj:`dict`
            batch of data; keys should include 'images' and 'masks', if necessary
        dataset : :obj:`int`, optional
            used for session-specific io layers
        accumulate_grad : :obj:`bool`, optional
            accumulate gradient for training step
        chunk_size : :obj:`int`, optional
            batch is split into chunks of this size to keep memory requirements low

        Returns
        -------
        :obj:`dict`
            - 'loss' (:obj:`float`): full elbo
            - 'loss_ll' (:obj:`float`): log-likelihood portion of elbo
            - 'loss_kl' (:obj:`float`): kl portion of elbo
            - 'loss_mse' (:obj:`float`): mse (without gaussian constants)
            - 'beta' (:obj:`float`): weight in front of kl term

        """

        x = data['images'][0]
        m = data['masks'][0] if 'masks' in data else None
        beta = self.beta_vals[self.curr_epoch]

        batch_size = x.shape[0]
        n_chunks = int(np.ceil(batch_size / chunk_size))

        loss_val = 0
        loss_ll_val = 0
        loss_kl_val = 0
        loss_mse_val = 0
        for chunk in range(n_chunks):

            idx_beg = chunk * chunk_size
            idx_end = np.min([(chunk + 1) * chunk_size, batch_size])

            x_in = x[idx_beg:idx_end]
            m_in = m[idx_beg:idx_end] if m is not None else None
            x_hat, _, mu, logvar = self.forward(x_in, dataset=dataset, use_mean=False)

            # log-likelihood
            loss_ll = losses.gaussian_ll(x_in, x_hat, m_in)

            # kl
            loss_kl = losses.kl_div_to_std_normal(mu, logvar)

            # combine
            loss = -loss_ll + beta * loss_kl

            if accumulate_grad:
                loss.backward()

            # get loss value (weighted by batch size)
            loss_val += loss.item() * (idx_end - idx_beg)
            loss_ll_val += loss_ll.item() * (idx_end - idx_beg)
            loss_kl_val += loss_kl.item() * (idx_end - idx_beg)
            loss_mse_val += losses.gaussian_ll_to_mse(
                loss_ll.item(), np.prod(x.shape[1:])) * (idx_end - idx_beg)

        loss_val /= batch_size
        loss_ll_val /= batch_size
        loss_kl_val /= batch_size
        loss_mse_val /= batch_size

        loss_dict = {
            'loss': loss_val, 'loss_ll': loss_ll_val, 'loss_kl': loss_kl_val,
            'loss_mse': loss_mse_val, 'beta': beta}

        return loss_dict


class ConvDecoder(BaseModel):
    """Decode images from predictors with a convolutional decoder."""

    def __init__(self, hparams):
        """

        Parameters
        ----------
        hparams : :obj:`dict`
            - 'model_type' (:obj:`int`): 'conv' | 'linear'
            - 'model_class' (:obj:`str`): 'conv-decoder'
            - 'y_pixels' (:obj:`int`)
            - 'x_pixels' (:obj:`int`)
            - 'n_input_channels' (:obj:`int`)
            - 'n_labels' (:obj:`int`)
            - 'fit_sess_io_layers; (:obj:`bool`): fit session-specific input/output layers
            - 'ae_decoding_x_dim' (:obj:`list`)
            - 'ae_decoding_y_dim' (:obj:`list`)
            - 'ae_decoding_n_channels' (:obj:`list`)
            - 'ae_decoding_kernel_size' (:obj:`list`)
            - 'ae_decoding_stride_size' (:obj:`list`)
            - 'ae_decoding_x_padding' (:obj:`list`)
            - 'ae_decoding_y_padding' (:obj:`list`)
            - 'ae_decoding_layer_type' (:obj:`list`)
            - 'ae_decoding_starting_dim' (:obj:`list`)
            - 'ae_decoding_last_FF_layer' (:obj:`bool`)

        """
        super(ConvDecoder, self).__init__()
        self.hparams = hparams
        self.model_type = self.hparams['model_type']
        self.img_size = (
                self.hparams['n_input_channels'],
                self.hparams['y_pixels'],
                self.hparams['x_pixels'])
        self.decoding = None
        self.build_model()

    def __str__(self):
        """Pretty print the model architecture."""
        format_str = '\nConvolutional decoder architecture\n'
        format_str += '------------------------\n'
        format_str += self.decoding.__str__()
        format_str += '\n'
        return format_str

    def build_model(self):
        """Construct the model using hparams."""
        self.hparams['hidden_layer_size'] = self.hparams['n_labels']
        if self.model_type == 'conv':
            from behavenet.models.aes import ConvAEDecoder
            self.decoding = ConvAEDecoder(self.hparams)
        elif self.model_type == 'linear':
            from behavenet.models.aes import LinearAEDecoder
            if self.hparams.get('fit_sess_io_layers', False):
                raise NotImplementedError
            self.decoding = LinearAEDecoder(self.hparams['n_labels'], self.img_size)
        else:
            raise ValueError('"%s" is an invalid model_type' % self.model_type)

    def forward(self, x, dataset=None, **kwargs):
        """Process input data.

        Parameters
        ----------
        x : :obj:`torch.Tensor` object
            input data
        dataset : :obj:`int`
            used with session-specific io layers

        Returns
        -------
        :obj:`tuple`
            - y (:obj:`torch.Tensor`): output of shape (n_frames, n_channels, y_pix, x_pix)
            - x (:obj:`torch.Tensor`): hidden representation of shape (n_frames, n_latents)

        """
        if self.model_type == 'conv':
            y = self.decoding(x, None, None, dataset=dataset)
        elif self.model_type == 'linear':
            y = self.decoding(x)
        else:
            raise ValueError('"%s" is an invalid model_type' % self.model_type)
        return y

    def loss(self, data, dataset=0, accumulate_grad=True, chunk_size=200):
        """Calculate MSE loss for convolutional decoder.

        The batch is split into chunks if larger than a hard-coded `chunk_size` to keep memory
        requirements low; gradients are accumulated across all chunks before a gradient step is
        taken.

        Parameters
        ----------
        data : :obj:`dict`
            batch of data; keys should include 'labels', 'images' and 'masks', if necessary
        dataset : :obj:`int`, optional
            used for session-specific io layers
        accumulate_grad : :obj:`bool`, optional
            accumulate gradient for training step
        chunk_size : :obj:`int`, optional
            batch is split into chunks of this size to keep memory requirements low

        Returns
        -------
        :obj:`dict`
            - 'loss' (:obj:`float`): mse loss

        """

        if self.hparams['device'] == 'cuda':
            data = {key: val.to('cuda') for key, val in data.items()}

        x = data['images'][0]
        y = data['labels'][0]
        m = data['masks'][0] if 'masks' in data else None

        batch_size = x.shape[0]
        n_chunks = int(np.ceil(batch_size / chunk_size))

        loss_val = 0
        for chunk in range(n_chunks):

            idx_beg = chunk * chunk_size
            idx_end = np.min([(chunk + 1) * chunk_size, batch_size])

            x_in = x[idx_beg:idx_end]
            y_in = y[idx_beg:idx_end]
            m_in = m[idx_beg:idx_end] if m is not None else None
            x_hat = self.forward(y_in, dataset=dataset)

            loss = losses.mse(x_in, x_hat, m_in)

            if accumulate_grad:
                loss.backward()

            # get loss value (weighted by batch size)
            loss_val += loss.item() * (idx_end - idx_beg)

        loss_val /= batch_size

        return {'loss': loss_val}
