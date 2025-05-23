# This file is part of sbi, a toolkit for simulation-based inference. sbi is licensed
# under the Apache License Version 2.0, see <https://www.apache.org/licenses/>

from typing import List, Optional, Tuple, Union

import torch
from torch import Tensor
from torch.distributions import Distribution

from sbi.inference.posteriors.base_posterior import NeuralPosterior
from sbi.inference.potentials.base_potential import BasePotential
from sbi.inference.potentials.posterior_based_potential import PosteriorBasedPotential
from sbi.sbi_types import Shape, TorchTransform
from sbi.utils.sbiutils import gradient_ascent, mcmc_transform
from sbi.utils.torchutils import ensure_theta_batched
from sbi.utils.user_input_checks import process_x


class EnsemblePosterior(NeuralPosterior):
    r"""Wrapper for bundling together different posterior instances into an ensemble.

    This class creates a posterior ensemble from a set of N different, already trained
    posterior estimators :math:`p_{i}(\theta|x_o)`, where :math:`i \in \{1,...,N\}`.

    It can wrap all posterior classes available in ``sbi`` and even a mixture of
    different posteriors, i.e. obtained via SNLE and SNPE at the same time, since it
    only provides a pass-through to the class methods of each posterior in the
    ensemble. The only constraint is that the individual posteriors have the same
    prior.

    So far, ``log_prob()``, ``sample()`` and ``map()`` functionality are supported.

    Attributes:
        posteriors: List of the posterior estimators making up the ensemble.
        num_components: Number of posterior estimators.
        weights: Weight of each posterior distribution. If none are provided each
            posterior is weighted with 1/N.
        priors: Prior distributions of all posterior components.
        theta_transform: If passed, this transformation will be applied during the
            optimization performed when obtaining the map. It does not affect the
            .sample() and .log_prob() methods.
        device: device to host the posterior distribution.

    Example:
    --------

    ::

        import torch
        from joblib import Parallel, delayed
        from sbi.examples.minimal import simple

        n_ensembles = 10
        posteriors = Parallel(n_jobs=-1)(delayed(simple)() for i in range(n_ensembles))

        ensemble = EnsemblePosterior(posteriors)
        ensemble.set_default_x(torch.zeros((3,)))
        ensemble.sample((1,))
    """

    def __init__(
        self,
        posteriors: List,
        weights: Optional[Union[List[float], Tensor]] = None,
        theta_transform: Optional[TorchTransform] = None,
        device: Optional[Union[str, torch.device]] = None,
    ):
        r"""
        Args:
            posteriors: List containing the trained posterior instances that will make
                up the ensemble.
            weights: Assign weights to posteriors manually, otherwise they will be
                weighted with 1/N.
            theta_transform: If passed, this transformation will be applied during the
                optimization performed when obtaining the map. It does not affect the
                `.sample()` and `.log_prob()` methods.
        """
        self.posteriors = posteriors
        self.num_components = len(posteriors)
        self.weights = weights
        self.theta_transform = theta_transform
        # Take first prior as reference
        self.prior = posteriors[0].potential_fn.prior
        self.device = device

        if self.device is None:
            self.device = self.ensure_same_device(posteriors)

        self._build_potential_fns()

    def to(self, device: Union[str, torch.device]) -> None:
        """Moves each posterior to device.

        Prior and weights are also moved to
        the specified device.

        Args:
            device: The device to move the ensemble posterior to.
        """
        self.device = device
        self._device = device
        for i in range(len(self.posteriors)):
            self.posteriors[i].to(device)
        self.prior.to(device)
        self.theta_transform = mcmc_transform(self.prior, device=device)
        self._weights.to(device)
        self._build_potential_fns()

    def _build_potential_fns(self):
        potential_fns = []
        for posterior in self.posteriors:
            potential = posterior.potential_fn
            potential_fns.append(potential)
            # make sure all prior are the same
            assert isinstance(potential.prior, type(self.prior)), (
                "All posteriors in ensemble must have the same prior: "
                f"{potential.prior} {self.prior}"
            )

        potential_fn = EnsemblePotential(potential_fns, self._weights, self.prior, None)

        super().__init__(
            potential_fn=potential_fn,
            theta_transform=self.theta_transform,
            device=self.device,
        )

    def ensure_same_device(self, posteriors: List) -> str:
        """Ensures that all posteriors in the ensemble are on the same device.

        Args:
            posteriors: List containing the trained posterior instances that will make
                up the ensemble.

        Raises:
            AssertionError if ensemble components have different device variables.

        Returns:
            A device string, that is the same for all posteriors.
        """
        devices = [posterior._device for posterior in posteriors]
        assert all(device == devices[0] for device in devices), (
            "Only supported if all posteriors are on the same device."
        )
        return devices[0]

    @property
    def weights(self) -> Tensor:
        return self._weights

    @weights.setter
    def weights(self, weights: Optional[Union[List[float], Tensor]]) -> None:
        """Set relative weight for each posterior in the ensemble.

        Weights are normalised.

        Raises:
            TypeError if weights are provided in an unsupported format.

        Args:
            weights: Assignes weight to each posterior distribution.
        """
        if weights is None:
            self._weights = torch.tensor([
                1.0 / self.num_components for _ in range(self.num_components)
            ])
        elif isinstance(weights, (Tensor, List)):
            self._weights = torch.tensor(weights) / sum(weights)
        else:
            raise TypeError

    def sample(
        self, sample_shape: Shape = torch.Size(), x: Optional[Tensor] = None, **kwargs
    ) -> Tensor:
        r"""Return samples from posterior ensemble.

        The samples are drawn according to their assigned weight. The number of samples
        for each distributino is drawn from a corresponding multinomial distribution.
        Then each component posterior is sampled individually and all samples are
        aggregated afterwards.

        All kwargs are passed directly through to `posterior.sample()`.

        Args:
            sample_shape: Desired shape of samples that are drawn from posterior
                ensemble. If sample_shape is multidimensional we simply draw
                `sample_shape.numel()` samples and then reshape into the desired shape.
            x: Conditioning context. If none is provided and no default context is set,
                an error will be raised.

        Returns:
            Samples drawn from the ensemble distribution.
        """
        num_samples = torch.Size(sample_shape).numel()
        posterior_indizes = torch.multinomial(
            self._weights, num_samples, replacement=True
        )
        samples = []
        for posterior_index, sample_size in torch.vstack(
            posterior_indizes.unique(return_counts=True)
        ).T:
            sample_shape_c = torch.Size((int(sample_size),))
            samples.append(
                self.posteriors[posterior_index].sample(sample_shape_c, x=x, **kwargs)
            )
        return torch.vstack(samples).reshape(*sample_shape, -1)

    def sample_batched(
        self,
        sample_shape: Shape,
        x: Tensor,
        **kwargs,
    ) -> Tensor:
        num_samples = torch.Size(sample_shape).numel()
        posterior_indices = torch.multinomial(
            self._weights, num_samples, replacement=True
        )
        samples = []
        for posterior_index, sample_size in torch.vstack(
            posterior_indices.unique(return_counts=True)
        ).T:
            sample_shape_c = torch.Size((int(sample_size),))
            samples.append(
                self.posteriors[posterior_index].sample_batched(
                    sample_shape_c, x=x, **kwargs
                )
            )
        samples = torch.vstack(samples)
        return samples.reshape(sample_shape + samples.shape[1:])

    def log_prob(
        self,
        theta: Tensor,
        x: Optional[Tensor] = None,
        individually: bool = False,
        **kwargs,
    ) -> Union[Tensor, Tuple[Tensor, Tensor]]:
        r"""Returns the average log-probability of the posterior ensemble

        $\sum_{i}^{N} w_{i} p_i(\theta|x)$.

        All kwargs are passed directly through to `posterior.log_prob()`.

        Args:
            theta: Parameters $\theta$.
            x: Conditioning context.If none is provided and no default context is set,
                an error will be raised.
            individually: If true, returns log weights and log_probs individually.

        Raises:
            AssertionError if posterior estimators are a mixture of different methods.

        Returns:
            `(len(θ),)`-shaped average log posterior probability $\log p(\theta|x)$ for
            θ in the support of the prior, -∞ (corresponding to 0 probability) outside.
        """
        assert all(
            isinstance(posterior, type(self.posteriors[0]))
            for posterior in self.posteriors
        ), "`log_prob()` only works for ensembles of the same type of posterior."

        log_probs = torch.stack([
            posterior.log_prob(theta, x=x, **kwargs) for posterior in self.posteriors
        ])
        log_weights = torch.log(self._weights).reshape(-1, 1)

        if individually:
            return log_weights, log_probs
        else:
            return torch.logsumexp(log_weights.expand_as(log_probs) + log_probs, dim=0)

    def set_default_x(self, x: Tensor) -> "NeuralPosterior":
        r"""Set new default x for `.sample(), .log_prob()` as conditioning context.

        This is a pure convenience to avoid having to repeatedly specify `x` in calls to
        `.sample()` and `.log_prob()` - only θ needs to be passed.

        This convenience is particularly useful when the posterior ensemble is focused,
        i.e. has been trained over multiple rounds to be accurate in the vicinity of a
        particular `x=x_o` (you can check if your posterior object is focused by
        printing one exemplary component of the ensemble).

        NOTE: this method is chainable, i.e. will return the EnsemblePosterior
        object so that calls like `posterior_enemble.set_default_x(my_x).sample(mytheta)
        ` are possible.

        Args:
            x: The default observation to set for every posterior $p_i(theta|x)$ in the
            ensemble.
        Returns:
            `EnsemblePosterior` that will use a default `x` when not explicitly
            passed.
        """
        self._x = process_x(x, x_event_shape=None).to(self._device)

        for posterior in self.posteriors:
            posterior.set_default_x(x)

        return self

    def potential(
        self, theta: Tensor, x: Optional[Tensor] = None, track_gradients: bool = False
    ) -> Tensor:
        r"""Evaluates $\theta$ under the potential that is used to sample the posterior.
        The potential is the unnormalized log-probability of $\theta$ under the
        posterior.
        Args:
            theta: Parameters $\theta$.
            track_gradients: Whether the returned tensor supports tracking gradients.
                This can be helpful for e.g. sensitivity analysis, but increases memory
                consumption.
        """
        self.potential_fn.set_x(self._x_else_default_x(x))

        theta = ensure_theta_batched(torch.as_tensor(theta))

        return self.potential_fn(
            theta.to(self._device), track_gradients=track_gradients
        )

    def map(
        self,
        x: Optional[Tensor] = None,
        num_iter: int = 1_000,
        num_to_optimize: int = 100,
        learning_rate: float = 0.01,
        init_method: Union[str, Tensor] = "posterior",
        num_init_samples: int = 1_000,
        save_best_every: int = 10,
        show_progress_bars: bool = False,
        individually: bool = False,
    ) -> Union[Tensor, Tuple[Tensor, Tensor]]:
        r"""Returns the average maximum-a-posteriori estimate (MAP).

        Computes MAP estimate across the whole ensemble or for each component
        individually. All args and kwargs are passed directly through to
        `gradient_ascent`.

        The routine can be interrupted (individually) with [Ctrl-C], when the user sees
        that the log-probability converges. The best estimate will be saved in `self.
        posteriors[idx].map_`.

        For more details of how the MAP estimate is obtained see `.map()` docstring of
        self.posteriors[idx].

        Args:
            x: Observed data at which to evaluate the MAP.
            num_iter: Number of optimization steps that the algorithm takes
                to find the MAP.
            num_to_optimize: From the drawn `num_init_samples`, use the
                `num_to_optimize` with highest log-probability as the initial points
            learning_rate: Learning rate of the optimizer.
            init_method: How to select the starting parameters for the optimization. If
                it is a string, it can be either [`posterior`, `prior`], which samples
                the respective distribution `num_init_samples` times. If it is a
                tensor, the tensor will be used as init locations.
            num_init_samples: Draw this number of samples from the posterior and
                evaluate the log-probability of all of them.
                for the optimization.
            save_best_every: The best log-probability is computed, saved in the
                `map`-attribute, and printed every `save_best_every`-th iteration.
                Computing the best log-probability creates a significant overhead
                (thus, the default is `10`.)
            show_progress_bars: Whether to show a progressbar during sampling from
                the posterior.
            individually: If true, returns log weights and MAPs individually.

        Returns:
            The ensemble MAP estimate or individual log_weigths and component MAP
            estimate if individually == True.
        """

        if individually:
            maps = []
            log_weights = torch.log(self._weights).reshape(-1, 1)

            for posterior in self.posteriors:
                maps.append(
                    posterior.map(
                        x=x,
                        num_iter=num_iter,
                        num_to_optimize=num_to_optimize,
                        learning_rate=learning_rate,
                        init_method=init_method,
                        num_init_samples=num_init_samples,
                        save_best_every=save_best_every,
                        show_progress_bars=show_progress_bars,
                    )
                )
            maps = torch.stack(maps)
            return log_weights, maps

        else:
            self.potential_fn.set_x(self._x_else_default_x(x))

            if init_method == "posterior":
                inits = self.sample((num_init_samples,), self._x_else_default_x(x))
            elif isinstance(init_method, Tensor):
                inits = init_method
            else:
                raise ValueError

            return gradient_ascent(
                potential_fn=self.potential_fn,
                inits=inits,
                theta_transform=self.theta_transform,
                num_iter=num_iter,
                num_to_optimize=num_to_optimize,
                learning_rate=learning_rate,
                save_best_every=save_best_every,
                show_progress_bars=show_progress_bars,
            )[0]


class EnsemblePotential(BasePotential):
    r"""Provides `EnsemblePosterior` with a `potential_fn` attribute.

    The potential is the same as the sum of the weighted log-probabilities of each
    component posterior.

    This class was modelled off of `PosteriorBasedPotential` and should provide similar
    functionality.

    Attributes:
        potential_fns: List of the potential_fns from each posterior component.
        weights: Weights of each posterior distribution.
    """

    def __init__(
        self,
        potential_fns: List,
        weights: Tensor,
        prior: Distribution,
        x_o: Optional[Tensor],
        device: Union[str, torch.device] = "cpu",
    ):
        r"""
        Args:
            potential_fns: List of the potential_fns from each posterior component.
            weights: Weights of each posterior distribution.
            priors: List of prior distributions.
            x_o: Used as conditioning context for `potential_fn`.
            device: Device which the component distributions sit on.
        """
        self._weights = weights
        self.potential_fns = potential_fns
        super().__init__(prior, x_o, device)

    def to(self, device: Union[str, torch.device]) -> None:
        """
        Moves the ensemble potentials, the prior, the weights and x_o to

        the specified device.

        Args:
            device: The device to move the ensemble potential to.
        """
        self.device = device
        for i in range(len(self.potential_fns)):
            self.potential_fns[i].to(device)
        self._weights = self._weights.to(device)
        self.prior.to(device)  # type: ignore
        if self._x_o is not None:
            self._x_o = self._x_o.to(device)

    def allow_iid_x(self) -> bool:
        # in case there is different kinds of posteriors, this will produce an error
        # in `set_x()`
        return any(
            isinstance(potential, PosteriorBasedPotential)
            for potential in self.potential_fns
        )

    def set_x(self, x_o: Optional[Tensor]):
        """Check the shape of the observed data and, if valid, set it."""
        if x_o is not None:
            x_o = process_x(x_o).to(  # type: ignore
                self.device
            )
        self._x_o = x_o
        for comp_potential in self.potential_fns:
            comp_potential.set_x(x_o)

    def __call__(self, theta: Tensor, track_gradients: bool = True) -> Tensor:
        r"""Returns the potential for posterior-based methods.

        Args:
            theta: The parameter set at which to evaluate the potential function.
            track_gradients: Whether to track the gradients.

        Returns:
            The potential.
        """
        theta = ensure_theta_batched(torch.as_tensor(theta))
        theta = theta.to(self.device)

        log_probs = [
            fn(theta, track_gradients=track_gradients) for fn in self.potential_fns
        ]
        log_probs = torch.vstack(log_probs)
        ensemble_log_probs = torch.logsumexp(
            torch.log(self._weights.reshape(-1, 1)).expand_as(log_probs) + log_probs,
            dim=0,
        )
        return ensemble_log_probs
