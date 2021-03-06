"""Custom losses for PyTorch models."""

import numpy as np
import torch
from torch.nn.modules.loss import _Loss
from torch.distributions.multivariate_normal import MultivariateNormal

# to ignore imports for sphix-autoapidoc
__all__ = [
    'mse', 'gaussian_ll', 'gaussian_ll_to_mse', 'kl_div_to_std_normal', 'index_code_mi',
    'total_correlation', 'dimension_wise_kl_to_std_normal', 'decomposed_kl', 'subspace_overlap']

LN2PI = np.log(2 * np.pi)


class GaussianNegLogProb(_Loss):
    """Minimize negative Gaussian log probability with learned covariance matrix.

    For now the covariance matrix is not data-dependent
    """

    def __init__(self, size_average=None, reduce=None, reduction='mean'):
        if reduction != 'mean':
            raise NotImplementedError
        super().__init__(size_average, reduce, reduction)

    def forward(self, input, target, precision):
        output_dim = target.shape[1]
        dist = MultivariateNormal(
            loc=input,
            covariance_matrix=1e-3 * torch.eye(output_dim) + precision)
        return torch.mean(-dist.log_prob(target))


def mse(y_pred, y_true, masks=None):
    """Compute mean square error (MSE) loss with masks.

    Parameters
    ----------
    y_pred : :obj:`torch.Tensor`
        predicted data
    y_true : :obj:`torch.Tensor`
        true data
    masks : :obj:`torch.Tensor`, optional
        binary mask that is the same size as `y_pred` and `y_true`; by placing 0 entries in the
        mask, the corresponding dimensions will not contribute to the loss term, and will therefore
        not contribute to parameter updates

    Returns
    -------
    :obj:`torch.Tensor`
        mean square error computed across all dimensions

    """
    if masks is not None:
        return torch.mean(((y_pred - y_true) ** 2) * masks)
    else:
        return torch.mean((y_pred - y_true) ** 2)


def gaussian_ll(y_pred, y_mean, masks=None, std=1):
    """Compute multivariate Gaussian log-likelihood with a fixed diagonal noise covariance matrix.

    Parameters
    ----------
    y_pred : :obj:`torch.Tensor`
        predicted data of shape (n_frames, ...)
    y_mean : :obj:`torch.Tensor`
        true data of shape (n_frames, ...)
    masks : :obj:`torch.Tensor`, optional
        binary mask that is the same size as `y_pred` and `y_true`; by placing 0 entries in the
        mask, the corresponding dimensions will not contribute to the loss term, and will therefore
        not contribute to parameter updates
    std : :obj:`float`, optional
        fixed standard deviation for all dimensions in the multivariate Gaussian

    Returns
    -------
    :obj:`torch.Tensor`
        Gaussian log-likelihood summed across dims, averaged across batch

    """
    dims = y_pred.shape
    n_dims = np.prod(dims[1:])  # first value is n_frames in batch
    log_var = np.log(std ** 2)

    if masks is not None:
        diff_sq = ((y_pred - y_mean) ** 2) * masks
    else:
        diff_sq = (y_pred - y_mean) ** 2

    ll = - (0.5 * LN2PI + 0.5 * log_var) * n_dims - (0.5 / (std ** 2)) * diff_sq.sum(
        axis=tuple(1+np.arange(len(dims[1:]))))

    return torch.mean(ll)


def gaussian_ll_to_mse(ll, n_dims, gaussian_std=1, mse_std=1):
    """Convert a Gaussian log-likelihood term to MSE by removing constants and swapping variances.

    - NOTE:
        does not currently return correct values if gaussian ll is computed with masks

    Parameters
    ----------
    ll : :obj:`float`
        original Gaussian log-likelihood
    n_dims : :obj:`int`
        number of dimensions in multivariate Gaussian
    gaussian_std : :obj:`float`
        std used to compute Gaussian log-likelihood
    mse_std : :obj:`float`
        std used to compute MSE

    Returns
    -------
    :obj:`float`
        MSE value

    """
    llc = np.copy(ll)
    llc += (0.5 * LN2PI + 0.5 * np.log(gaussian_std ** 2)) * n_dims  # remove constant
    llc *= -(gaussian_std ** 2) / 0.5  # undo scaling by variance
    llc /= n_dims  # change sum to mean
    llc *= 1.0 / (mse_std ** 2)  # scale by mse variance
    return llc


def kl_div_to_std_normal(mu, logvar):
    """Compute element-wise KL(q(z) || N(0, 1)) where q(z) is a normal parameterized by mu, logvar.

    Parameters
    ----------
    mu : :obj:`torch.Tensor`
        mean parameter of shape (n_frames, n_dims)
    logvar
        log variance parameter of shape (n_frames, n_dims)

    Returns
    -------
    :obj:`torch.Tensor`
        KL divergence summed across dims, averaged across batch

    """
    kl = 0.5 * torch.sum(logvar.exp() - logvar + mu.pow(2) - 1, dim=1)
    return torch.mean(kl)


def index_code_mi(z, mu, logvar):
    """Estimate index code mutual information in a batch.

    We ignore the constant as it does not matter for the minimization. The constant should be
    equal to log(n_frames * dataset_size).

    Parameters
    ----------
    z : :obj:`torch.Tensor`
        sample of shape (n_frames, n_dims)
    mu : :obj:`torch.Tensor`
        mean parameter of shape (n_frames, n_dims)
    logvar : :obj:`torch.Tensor`
        log variance parameter of shape (n_frames, n_dims)

    Returns
    -------
    :obj:`torch.Tensor`
        index code mutual information for batch, scalar value

    """
    # Compute log(q(z(x_j)|x_i)) for every sample/dimension in the batch, which is a tensor of
    # shape (n_frames, n_dims). In the following comments,
    # (n_frames, n_frames, n_dims) are indexed by [j, i, l].
    # z[:, None]: (n_frames, 1, n_dims)
    # mu[None, :]: (1, n_frames, n_dims)
    # logvar[None, :]: (1, n_frames, n_dims)
    log_qz_prob = _gaussian_log_density_unsummed(z[:, None], mu[None, :], logvar[None, :])

    # Compute log(q(z(x_j))) as log(sum_i(q(z(x_j)|x_i))) + constant =
    # log(sum_i(prod_l q(z(x_j)_l|x_i))) + constant.
    log_qz = torch.logsumexp(
        torch.sum(log_qz_prob, dim=2, keepdim=False),  # sum over gaussian dims
        dim=1,  # logsumexp over batch
        keepdim=False)

    # Compute log prod_l q(z(x_j)_l | x_j) = sum_l log q(z(x_j)_l | x_j)
    log_qz_ = torch.diag(torch.sum(log_qz_prob, dim=2, keepdim=False))  # sum over gaussian dims

    return torch.mean(log_qz_ - log_qz)


def total_correlation(z, mu, logvar):
    """Estimate total correlation in a batch.

    Compute the expectation over a batch of:

    E_j [log(q(z(x_j))) - log(prod_l q(z(x_j)_l))]

    We ignore the constant as it does not matter for the minimization. The constant should be
    equal to (n_dims - 1) * log(n_frames * dataset_size).

    Code modified from https://github.com/julian-carpenter/beta-TCVAE/blob/master/nn/losses.py

    Parameters
    ----------
    z : :obj:`torch.Tensor`
        sample of shape (n_frames, n_dims)
    mu : :obj:`torch.Tensor`
        mean parameter of shape (n_frames, n_dims)
    logvar : :obj:`torch.Tensor`
        log variance parameter of shape (n_frames, n_dims)

    Returns
    -------
    :obj:`torch.Tensor`
        total correlation for batch, scalar value

    """
    # Compute log(q(z(x_j)|x_i)) for every sample/dimension in the batch, which is a tensor of
    # shape (n_frames, n_dims). In the following comments,
    # (n_frames, n_frames, n_dims) are indexed by [j, i, l].
    # z[:, None]: (n_frames, 1, n_dims)
    # mu[None, :]: (1, n_frames, n_dims)
    # logvar[None, :]: (1, n_frames, n_dims)
    log_qz_prob = _gaussian_log_density_unsummed(z[:, None], mu[None, :], logvar[None, :])

    # Compute log prod_l p(z(x_j)_l) = sum_l(log(sum_i(q(z(x_j)_l|x_i))) + constant) for each
    # sample in the batch, which is a vector of size (batch_size,).
    log_qz_product = torch.sum(
        torch.logsumexp(log_qz_prob, dim=1, keepdim=False),  # logsumexp over batch
        dim=1,  # sum over gaussian dims
        keepdim=False)

    # Compute log(q(z(x_j))) as log(sum_i(q(z(x_j)|x_i))) + constant =
    # log(sum_i(prod_l q(z(x_j)_l|x_i))) + constant.
    log_qz = torch.logsumexp(
        torch.sum(log_qz_prob, dim=2, keepdim=False),  # sum over gaussian dims
        dim=1,  # logsumexp over batch
        keepdim=False)

    return torch.mean(log_qz - log_qz_product)


def dimension_wise_kl_to_std_normal(z, mu, logvar):
    """Estimate dimensionwise KL divergence to standard normal in a batch.

    Parameters
    ----------
    z : :obj:`torch.Tensor`
        sample of shape (n_frames, n_dims)
    mu : :obj:`torch.Tensor`
        mean parameter of shape (n_frames, n_dims)
    logvar : :obj:`torch.Tensor`
        log variance parameter of shape (n_frames, n_dims)

    Returns
    -------
    :obj:`torch.Tensor`
        dimension-wise KL to standard normal for batch, scalar value

    """
    # Compute log(q(z(x_j)|x_i)) for every sample/dimension in the batch, which is a tensor of
    # shape (n_frames, n_dims). In the following comments,
    # (n_frames, n_frames, n_dims) are indexed by [j, i, l].
    # z[:, None]: (n_frames, 1, n_dims)
    # mu[None, :]: (1, n_frames, n_dims)
    # logvar[None, :]: (1, n_frames, n_dims)
    log_qz_prob = _gaussian_log_density_unsummed(z[:, None], mu[None, :], logvar[None, :])

    # Compute log prod_l p(z(x_j)_l) = sum_l(log(sum_i(q(z(x_j)_l|x_i))) + constant) for each
    # sample in the batch, which is a vector of size (batch_size,).
    log_qz_product = torch.sum(
        torch.logsumexp(log_qz_prob, dim=1, keepdim=False),  # logsumexp over batch
        dim=1,  # sum over gaussian dims
        keepdim=False)

    # Compute
    log_pz_prob = _gaussian_log_density_unsummed_std_normal(z)
    log_pz_product = torch.sum(log_pz_prob, dim=1, keepdim=False)  # sum over gaussian dims

    return torch.mean(log_qz_product - log_pz_product)


def decomposed_kl(z, mu, logvar):
    """Decompose KL term in VAE loss.

    Decomposes the KL divergence loss term of the variational autoencoder into three terms:
    1. index code mutual information
    2. total correlation
    3. dimension-wise KL

    None of these terms can be computed exactly when using stochastic gradient descent. This
    function instead computes approximations as detailed in https://arxiv.org/pdf/1802.04942.pdf.

    Parameters
    ----------
    z : :obj:`torch.Tensor`
        sample of shape (n_frames, n_dims)
    mu : :obj:`torch.Tensor`
        mean parameter of shape (n_frames, n_dims)
    logvar : :obj:`torch.Tensor`
        log variance parameter of shape (n_frames, n_dims)

    Returns
    -------
    :obj:`tuple`
        - index code mutual information (:obj:`torch.Tensor`)
        - total correlation (:obj:`torch.Tensor`)
        - dimension-wise KL (:obj:`torch.Tensor`)

    """

    # Compute log(q(z(x_j)|x_i)) for every sample/dimension in the batch, which is a tensor of
    # shape (n_frames, n_dims). In the following comments, (n_frames, n_frames, n_dims) are indexed
    # by [j, i, l].
    #
    # Note that the insertion of `None` expands dims to use torch's broadcasting feature
    # z[:, None]: (n_frames, 1, n_dims)
    # mu[None, :]: (1, n_frames, n_dims)
    # logvar[None, :]: (1, n_frames, n_dims)
    log_qz_prob = _gaussian_log_density_unsummed(z[:, None], mu[None, :], logvar[None, :])

    # Compute log(q(z(x_j))) as
    # log(sum_i(q(z(x_j)|x_i))) + constant
    # = log(sum_i(prod_l q(z(x_j)_l|x_i))) + constant
    # = log(sum_i(exp(sum_l log q(z(x_j)_l|x_i))) + constant (assumes q is factorized)
    log_qz = torch.logsumexp(
        torch.sum(log_qz_prob, dim=2, keepdim=False),  # sum over gaussian dims
        dim=1,  # logsumexp over batch
        keepdim=False)

    # Compute log prod_l q(z(x_j)_l | x_j)
    # = sum_l log q(z(x_j)_l | x_j)
    log_qz_ = torch.diag(torch.sum(log_qz_prob, dim=2, keepdim=False))  # sum over gaussian dims

    # Compute log prod_l p(z(x_j)_l)
    # = sum_l(log(sum_i(q(z(x_j)_l|x_i))) + constant
    log_qz_product = torch.sum(
        torch.logsumexp(log_qz_prob, dim=1, keepdim=False),  # logsumexp over batch
        dim=1,  # sum over gaussian dims
        keepdim=False)

    # Compute sum_l log p(z(x_j)_l)
    log_pz_prob = _gaussian_log_density_unsummed_std_normal(z)
    log_pz_product = torch.sum(log_pz_prob, dim=1, keepdim=False)  # sum over gaussian dims

    idx_code_mi = torch.mean(log_qz_ - log_qz)
    total_corr = torch.mean(log_qz - log_qz_product)
    dim_wise_kl = torch.mean(log_qz_product - log_pz_product)

    return idx_code_mi, total_corr, dim_wise_kl


def _gaussian_log_density_unsummed(z, mu, logvar):
    """First step of Gaussian log-density computation, without summing over dimensions.

    Assumes a diagonal noise covariance matrix.

    """
    diff_sq = (z - mu) ** 2
    inv_var = torch.exp(-logvar)
    return - 0.5 * (inv_var * diff_sq + logvar + LN2PI)


def _gaussian_log_density_unsummed_std_normal(z):
    """First step of Gaussian log-density computation, without summing over dimensions.

    Assumes a diagonal noise covariance matrix.

    """
    diff_sq = z ** 2
    return - 0.5 * (diff_sq + LN2PI)


def subspace_overlap(A, B):
    """Compute inner product between subspaces defined by matrices A and B.

    Parameters
    ----------
    A : :obj:`torch.Tensor`
        shape (a, b)
    B : :obj:`torch.Tensor`
        shape (c, b)

    Returns
    -------
    :obj:`torch.Tensor`
        scalar value; Frobenious norm of AB^T divided by number of entries

    """
    C = torch.cat([A, B], dim=0)
    d = C.shape[0]
    eye = torch.eye(d, device=C.device)
    return torch.mean((torch.matmul(C, torch.transpose(C, 1, 0)) - eye).pow(2))
    # return torch.mean(torch.matmul(A, torch.transpose(B, 1, 0)).pow(2))
