import operator
from cachetools import cachedmethod, Cache
import numpy as np
from scipy.stats import beta, gamma, norm

from bss import logger
from bss.mvn import Mvn
from bss.samplers.slice import SliceSampler
from bss.samplers.elliptical import EllipticalSliceSampler


class Probit:
    def __init__(self, X, Y, R, target_sparsity=0.01, gamma0_v=1.0, lambda_params=(1e-6, 1e-6), nu_params=(1e-6, 1e-6),
                 xi=0.999999, xi_prior_shape=(1, 1), check_finite=True, min_eigenval=0, jitter=1e-6):
        """The Probit model used for modeling Sparse Regression using a Gaussian field. :cite:`Engelhardt2014`.

        .. math::

            y|X,\\beta,\\beta_0, \\nu \propto \mathcal{N}(\\beta_0 1_n + X \\beta, \\nu^{-1} I_n)

        Parameters
        ----------
        X : ndarray
           The predictor matrix of real numbers, n x p in size, where n is the no. of samples (genotypes) and p is the
           no. of features (SNPs).
        Y : ndarray
           The response vector of real numbers, n x 1 in size, with each value representing the phenotype value for the
           sample.
        R : ndarray
           The covariance matrix for the SNPs, p x p in size. The matrix may not be positive-definite, but is converted
           to one internally.
        target_sparsity : float
            The proportion of included predictors. For example, a value of 0.01 indicates that around 1% of total SNPs
            are expected be included in our model. This value affects the probit threshold gamma_0 of the model.
        gamma0_v : float
            Variance of the probit threshold gamma_0
        lambda_params : tuple
            Shape parameter and Inverse-scale parameter of the gamma prior placed on the model parameter lambda, where
            lambda is the inverse squared global scale parameter for the regression weights.
        nu_params : tuple
            Shape parameter and Inverse-scale parameter of the gamma prior placed on the model parameter nu, where nu
            is the residual precision.
        xi : float
            The shrinkage constant in the interval [0,1] to regularize the covariance matrix towards the identity
            matrix. This ensures that the covariance matrix is positive definite.
            A larger xi value biases our estimate towards the supplied R matrix, a lower value biases it towards the
            identity matrix.
            If None, then xi is sampled from a beta distribution with shape parameters specified by the tuple
            xi_prior_shape.
        xi_prior_shape : tuple
            Shape parameters of the beta prior placed on the model parameter xi, specified as a 2-tuple of real values.
            This argument is ignored and xi is not sampled, if it is specified explicitly using the xi parameter.
        check_finite : bool
            Whether to check that the input matrices contain only finite numbers. Disabling may give a performance gain,
            but may result in problems (crashes, non-termination) if the inputs do contain infinities or NaNs.
            This parameter is passed on to several linear algebra functions in scipy internally.
        min_eigenval : float
            Minimum Eigenvalue we can accept in the covariance matrix. Any eigenvalues encountered below this threshold
            are set to zero, and the resulting covariance matrix normalized to give ones on the diagonal.
        jitter : float
            A small value to add to the diagonals of the covariance matrix to avoid conditioning issues.
        """

        self.X = X
        self.Y = Y
        self.R = Mvn(cov=R, min_eigenval=min_eigenval, jitter=jitter)

        self.N, self.P = self.X.shape

        self.nu_a, self.nu_b = nu_params

        self.check_finite = check_finite

        if xi is None:
            self.sample_xi = True
            self._xi_distribution = beta(*xi_prior_shape)
            self.xi = self._xi_distribution.mean()
        else:
            self.sample_xi = False
            self.xi = xi

        # Initialize scalar model distributions and the parameter values to their prior means.
        self._gamma0_distribution = norm(loc=norm.ppf(1.0 - target_sparsity), scale=gamma0_v)
        self.gamma0 = self._gamma0_distribution.mean()
        self._lambda_distribution = gamma(lambda_params[0], scale=1./lambda_params[1])
        self.lamb = self._lambda_distribution.mean()
        self._nu_distribution = gamma(self.nu_a, scale=1./self.nu_b)
        self.nu = self._nu_distribution.mean()

        # Cache for holding probit prior distributions (multivariate normal distributions with 0 mean and known
        # covariance, possibly adjusted by a shrinkage factor xi expressing our confidence in the covariance).
        # A single iteration of MCMC calls on many computations on this distribution, so caching improves performance
        # significantly. A small cache size works just as well as a large one,
        # because the most recently used distribution tends to be used repeatedly in a single MCMC step.
        self._probit_cache = Cache(maxsize=4)

        # A cache used to hold the marginal PPI (Posterior Probability of Inclusion) distributions
        # p(y | X, gamma, gamma_0, nu, lambda) ~ Normal(..)
        # A small cache size works just as well as a large one, because the most recently used distribution tends to
        # be used repeatedly in a single MCMC step.
        self._ppi_cache = Cache(maxsize=8)

        # Initialize the sparsity function by generating a random variate from the model's probit distribution
        self.gamma = self.probit_distribution(self.xi).rvs()

    def _cache_key(self, *args):
        """
        A unique key for given arguments args, useful for indexing into a cache. Each argument should either be
        already hashable (e.g. a primitive Python types), or an ndarray (which we make hashable here)
        """
        return hash(tuple(hash(arg.data.tobytes()) if isinstance(arg, np.ndarray) else arg for arg in args))

    @cachedmethod(cache=operator.attrgetter('_probit_cache'), key=_cache_key)
    def probit_distribution(self, xi):
        """
        The probit distribution of the model, driven by a latent Gaussian field.

        Parameters
        ----------
        xi : float
            The shrinkage constant in the interval [0,1] to regularize the covariance matrix towards the identity
            matrix. This ensures that the covariance matrix is positive definite.
            A larger xi value biases our estimate towards the supplied R matrix, a lower value biases it towards the
            identity matrix.

        Returns
        -------
        The normal distribution of the probits (`gamma`) of the model.
        """
        return Mvn(cov=(xi * self.R.cov) + (1.0 - xi) * np.eye(self.P))

    @cachedmethod(cache=operator.attrgetter('_ppi_cache'), key=_cache_key)
    def ppi_distribution(self, gamma, gamma0, lamb):
        """
        Normal distribution for the posterior probability of inclusion.

        Parameters
        ----------
        gamma : ndarray
            A dx1 vector of probit values, one per SNP
        gamma0 : float
            The probit threshold, All SNPs with gamma > gamma0 are 'activated'.
        lamb : float
            Inverse squared global scale parameter for the regression weights.

        Returns
        -------
        Mvn
            A multivariate normal distribution object representing the posterior probability of inclusion

        Notes
        -----
        We're interested in the posterior probability of inclusion:
            .. math::
                p(y|X,\gamma,\gamma_0,\\nu,\lambda)

        marginalizing out the effect size captured by :math:`\\beta`:
            .. math::
                \\beta | \\nu,\lambda,\Gamma \propto \mathcal{N}(0, (\\nu\lambda)^{-1}\Gamma)

        The degenerate Gaussian form of the :math:`\\beta` prior above (note that the covariance matrix :math:`\Gamma`
        is a diagonal matrix of indicator values) allows us to perform this marginalization in closed form:
            .. math::
                p(y|X,\gamma,\gamma_0,\\nu,\lambda)

                = \int \int \mathcal{N} (y|\\beta_01_n + X\\beta,\\nu^{-1}I_n \;
                \mathcal{N} (\\beta|0, (\\nu\lambda)^{-1}\Gamma)) \;
                \mathcal{N}(\\beta_0|0,(\\nu\lambda)^{-1}) \; d\\beta d\\beta_0

                = \int \mathcal{N} (y|\\beta_0 1_n, \\nu^{-1}(\lambda^{-1}X\Gamma X^T + I_n)) \;
                \mathcal{N}(\\beta_0 | 0, (\\nu\lambda)^{-1}) d\\beta_0

                = \mathcal{N} (y|0, \\nu^{-1} \lambda^{-1}(1_n1_n^T + X\Gamma X^T) + I_n))


        Implementation Details
        ----------------------
        The natural way to implement this would be:

        indicator_matrix = np.diag(gamma > gamma0)
        result = Mvn(
            cov = (self.X.dot(indicator_matrix).dot(self.X.T) + np.ones((self.N, self.N))) / lamb
            + np.eye(self.N)
        )

        However:
          X * indicator_matrix * X.T
        is an expensive matrix multiplication, which can be avoided by first taking the columns of X that are above
        the probit threshold gamma0, and then simply taking the square of that masked matrix:
          X = X[:, gamma > gamma0]
          X * X.T
        """
        X = self.X[:, gamma > gamma0]
        return Mvn(cov=(np.dot(X, X.T) + np.ones((self.N, self.N))) / lamb + np.eye(self.N))

    def log_marg_like(self, gamma, gamma0, lamb, nu):
        r"""
        The marginal likelihood log-value of given model parameters.

        Parameters
        ----------
        gamma : ndarray
            A dx1 vector of probit values, one per SNP
        gamma0 : float
            The probit threshold, All SNPs with gamma > gamma0 are 'activated'.
        lamb : float
            Inverse squared global scale parameter for the regression weights.
        nu : float
            The residual precision of the model.

        Returns
        -------
        float
            A scalar log-likelihood value of the model, given the model parameters (and the true Y values stored in
            this model object).
        """
        return self.ppi_distribution(gamma, gamma0, lamb).logpdf(self.Y, precision_multiplier=nu)

    def log_joint(self):
        r"""
        The joint log-likelihood value of given model parameters.

        Returns
        -------
        float
            A scalar joint log-likelihood value of the entire model, given the model parameters and the true Y values
            stored in this model object.
        """
        return sum([
            self.log_marg_like(self.gamma, self.gamma0, self.lamb, self.nu),
            self._gamma0_distribution.logpdf(self.gamma0),
            self._nu_distribution.logpdf(self.nu),
            self._lambda_distribution.logpdf(self.lamb),
            self.probit_distribution(self.xi).logpdf(self.gamma),
            self._xi_distribution.logpdf(self.xi) if self.sample_xi else 0.0
        ])

    def run_mcmc(self, iters=1000, burn_in=100, detailed=False):
        r"""
        Execute a run of MCMC on this model given the current state of model parameters.

        Parameters
        ----------
        iters : int
            The total no. of iterations (excluding the burn-in) to run the MCMC simulations.
        burn_in : int
            The total no. of burn-in iterations of the MCMC simulation.
        detailed : bool, optional
            Whether to return a detailed trace of individual model parameters, or a trace of the joint log-likelihood
            value.

        Returns
        -------
        list, dictionary
            A list of scalars (joint log-likelihood value of the entire model), or a dictionary of lists (with
            keys `inclusion, gamma0, lambda, nu, xi, joint, likelihood`), and traces of individual model parameters as
            values in the dictionary).
        """
        logjoint_trace = np.zeros(iters)
        if detailed:
            inclusion_trace = np.zeros((iters, self.P), dtype=bool)
            gamma0_trace = np.zeros(iters)
            lambda_trace = np.zeros(iters)
            nu_trace = np.zeros(iters)
            xi_trace = np.zeros(iters)
            loglike_trace = np.zeros(iters)

        for i in range(-burn_in, iters):
            log_joint = self.log_joint()
            log_like = self.log_marg_like(self.gamma, self.gamma0, self.lamb, self.nu)

            logger.info(
                '%05d / %05d] logprob: %f [gamma0:%f lambda:%f nu:%f xi:%f' %
                (i, iters, log_joint, self.gamma0, self.lamb, self.nu, self.xi)
            )

            self.update_parameters()

            if i >= 0:
                logjoint_trace[i] = log_joint
                if detailed:
                    inclusion_trace[i, :] = self.gamma > self.gamma0
                    gamma0_trace[i] = self.gamma0
                    lambda_trace[i] = self.lamb
                    nu_trace[i] = self.nu
                    xi_trace[i] = self.xi
                    loglike_trace[i] = log_like

        if not detailed:
            return logjoint_trace
        else:
            return {
                'inclusion': (np.mean(inclusion_trace, 0), inclusion_trace[np.argmax(logjoint_trace), :]),
                'gamma0': gamma0_trace,
                'lambda': lambda_trace,
                'nu': nu_trace,
                'xi': xi_trace,
                'joint': logjoint_trace,
                'likelihood': loglike_trace
            }

    def update_parameters(self):
        r"""
        Update all model parameters in a single MCMC step.

        Returns
        -------
        On return, the gamma, gamma0, lambda, nu and optionally the `xi` model parameter have been updated.
        """
        # We update gamma, gamma0, lambda and nu in turn (Bottolo et al, 2011)
        self.update_gamma()
        self.update_gamma0()
        self.update_lambda()
        self.update_nu()
        if self.sample_xi:
            self.update_xi()

    def update_gamma(self):
        r"""
        Apply MCMC transition operator to model parameter :math:`\gamma`. For updating :math:`\gamma`, we use
        elliptical slice sampling (ESS) described in :cite:`Murray2010`

        Returns
        -------
            On return, the :math:`\gamma` parameter of the model has been updated using a new sample.

        Notes
        -----
        ESS samples efficiently and robustly from latent Gaussian models when significant covariance structure is
        imposed by the prior, as in the Gaussian processes and the present structured sparsity model.

        ESS generates random elliptical loci using hte Gaussian prior and then searches along these loci to find
        acceptable points for slice sampling. When the data ar weakly informative and the prior is strong, as is the
        case here, the elliptical loci effectively captures the dependence between the variables and enable faster
        mixing. Here, using ESS for :math:`\gamma` enables us to avoid directly sampling over the large discrete
        space of sparsity patterns that makes unstructured spike-and-slab computationally challenging.
        """
        self.gamma = EllipticalSliceSampler(
            normal_dist=self.probit_distribution(self.xi),
            log_like_fn=lambda gamma: self.log_marg_like(gamma, self.gamma0, self.lamb, self.nu)
        ).one(x0=self.gamma)

    def update_nu(self):
        r"""
        Apply MCMC transition operator to model parameter :math:`\nu`, the residual precision.

        Returns
        -------
            On return, the :math:`\nu` parameter of the model has been updated using this analytical approach.

        Notes
        -----
        The scalar nu determines the precision of the residual Gaussian noise of the response variables.
        With the choice of a conjugate gamma prior distribution, the conditional posterior is also gamma:

        .. math::

            p(\nu | y, X, \Gamma, \lambda) \propto
            \mathcal{N}(y|0,\nu^{-1}(\lambda^{-1}(1_n1_n^T + X \Gamma X^T) + I_n)) \, Gam(\nu|a_\nu, b_\nu)

            = Gam(\nu | a_\nu^{(n)}, b_\nu^{(n)})

            a_\nu^{(n)} = a_\nu + \frac{N}{2}

            b_\nu^{(n)} = b_\nu + \frac{1}{2} y^T (\lambda^{-1} (1_n 1_n^T + X \Gamma X^T) + I_n)^{-1} y
        """
        ppi_distribution = self.ppi_distribution(self.gamma, self.gamma0, self.lamb)

        distance_sq = ppi_distribution.maha(self.Y)
        post_a = self.nu_a + 0.5 * self.N
        post_b = self.nu_b + 0.5 * distance_sq

        self.nu = np.random.gamma(post_a, 1 / post_b)

    def update_lambda(self):
        r"""
        Apply MCMC transition operator to model parameter :math:`\lambda`, the global weight inverse-scale parameter.

        Returns
        -------
        On return, the :math:`\lambda` parameter of the model has been updated using a new sample.

        Notes
        -----
        The parameter :math:`\lambda` determines the scale of the "slab" portion of the weight prior. The conditional
        density of :math:`\lambda` does not have a simple closed form, but can be efficiently sampled using the
        exponential-expansion slice sampling algorithm described in :cite:`Neal2003`
        """
        def slice_fn(lamb):
            if lamb < 0:
                return -np.inf
            return self.log_marg_like(self.gamma, self.gamma0, lamb, self.nu) + self._lambda_distribution.logpdf(lamb)

        self.lamb = SliceSampler(slice_fn).one(x0=self.lamb)

    def update_gamma0(self):
        r"""
        Apply MCMC transition operator to the model parameter :math:`\gamma_0`, the sparsity threshold.

        Returns
        -------
            On return, the :math:`\gamma_0` parameter of the model has been updated using a new sample.

        Notes
        -----
        The parameter :math:`\gamma_0` specifies the probit threshold and, conditioned on :math:`\gamma`, it determines
        which entries on the diagonal of :math:`\Gamma` are zero and which are one. The conditional
        density of :math:`\gamma_0` does not have a simple closed form, but can be efficiently sampled using the
        exponential-expansion slice sampling algorithm described in :cite:`Neal2003`
        """
        def slice_fn(gamma0):
            return self.log_marg_like(self.gamma, gamma0, self.lamb, self.nu) + self._gamma0_distribution.logpdf(gamma0)

        self.gamma0 = SliceSampler(slice_fn).one(x0=self.gamma0)

    def update_xi(self):
        r"""
        Apply MCMC transition operator to the shrinkage factor :math:`\xi`, used to regularize the covariance matrix
        towards the identity matrix.

        Returns
        -------
        On return, the :math:`\xi` parameter of the model has been updated using a new sample.
        """

        # Compute the latent whitened variables
        whitened = self.probit_distribution(self.xi).whiten(self.gamma)

        def slice_fn(xi):
            if xi <= 0 or xi >= 1:
                return -np.inf

            try:
                gamma = self.probit_distribution(xi).correlate(whitened)
            except np.linalg.linalg.LinAlgError:
                return -np.inf
            else:
                return self.log_marg_like(gamma, self.gamma0, self.lamb, self.nu) + self._xi_distribution.logpdf(xi)

        self.xi = SliceSampler(slice_fn).one(x0=self.xi)
        self.gamma = self.probit_distribution(self.xi).correlate(whitened)
