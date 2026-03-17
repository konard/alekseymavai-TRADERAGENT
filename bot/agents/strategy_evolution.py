"""Strategy Evolution -- genetic algorithm for evolving strategy parameters."""

from __future__ import annotations

import random
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class Genome:
    """A set of strategy parameters (chromosome)."""

    genome_id: str = field(default_factory=lambda: f"g_{uuid.uuid4().hex[:8]}")
    parameters: dict[str, float] = field(default_factory=dict)
    fitness: float = 0.0  # Sharpe ratio or other metric
    generation: int = 0
    parent_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "genome_id": self.genome_id,
            "parameters": dict(self.parameters),
            "fitness": self.fitness,
            "generation": self.generation,
            "parent_ids": list(self.parent_ids),
        }


@dataclass
class EvolutionResult:
    generations_run: int
    population_size: int
    best_genome: Genome
    best_fitness: float
    fitness_history: list[float]  # best fitness per generation
    avg_fitness_history: list[float]  # average fitness per generation
    parameter_convergence: dict[str, list[float]]  # param_name -> values over generations
    total_evaluations: int
    elapsed_seconds: float

    def to_dict(self) -> dict:
        return {
            "generations_run": self.generations_run,
            "population_size": self.population_size,
            "best_genome": self.best_genome.to_dict(),
            "best_fitness": self.best_fitness,
            "fitness_history": list(self.fitness_history),
            "avg_fitness_history": list(self.avg_fitness_history),
            "parameter_convergence": {k: list(v) for k, v in self.parameter_convergence.items()},
            "total_evaluations": self.total_evaluations,
            "elapsed_seconds": self.elapsed_seconds,
        }


class StrategyEvolver:
    """Genetic algorithm for optimizing strategy parameters.

    Evolves parameters like:
    - EMA periods (fast, slow)
    - Grid spacing, grid levels
    - Stop-loss / Take-profit ratios
    - ADX thresholds
    - Risk per trade
    - RSI overbought/oversold levels

    Fitness function: user-provided callable that takes parameters dict
    and returns a fitness score (higher = better). Typically Sharpe ratio.

    Operators:
    - Selection: tournament selection (pick best of K random)
    - Crossover: uniform crossover (randomly pick each param from parent A or B)
    - Mutation: Gaussian perturbation with adaptive mutation rate
    """

    def __init__(
        self,
        parameter_ranges: dict[str, tuple[float, float]],
        population_size: int = 30,
        elite_count: int = 3,
        mutation_rate: float = 0.2,
        mutation_sigma: float = 0.1,
        crossover_rate: float = 0.7,
        tournament_size: int = 3,
        seed: int | None = None,
    ) -> None:
        self._parameter_ranges = parameter_ranges
        self._population_size = population_size
        self._elite_count = elite_count
        self._mutation_rate = mutation_rate
        self._mutation_sigma = mutation_sigma
        self._crossover_rate = crossover_rate
        self._tournament_size = tournament_size

        self._rng = random.Random(seed)
        self._population: list[Genome] = []
        self._generation: int = 0
        self._best_genome: Genome | None = None
        self._fitness_history: list[float] = []
        self._avg_fitness_history: list[float] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def initialize_population(self) -> list[Genome]:
        """Create initial random population within parameter ranges."""
        self._population = []
        for _ in range(self._population_size):
            params: dict[str, float] = {}
            for name, (lo, hi) in self._parameter_ranges.items():
                params[name] = self._rng.uniform(lo, hi)
            genome = Genome(
                genome_id=f"g_{uuid.UUID(int=self._rng.getrandbits(128)).hex[:8]}",
                parameters=params,
                generation=0,
            )
            self._population.append(genome)
        return list(self._population)

    def evolve(
        self,
        fitness_fn: Callable[[dict[str, float]], float],
        generations: int = 20,
        progress_callback: Callable | None = None,
    ) -> EvolutionResult:
        """Run the genetic algorithm.

        For each generation:
          1. Evaluate fitness for all genomes
          2. Sort by fitness
          3. Keep elite_count best unchanged
          4. Fill rest via: select parents -> crossover -> mutate
          5. Record best and average fitness
        """
        if not self._population:
            self.initialize_population()

        t0 = time.monotonic()
        total_evaluations = 0
        parameter_convergence: dict[str, list[float]] = {
            name: [] for name in self._parameter_ranges
        }

        for gen in range(generations):
            self._generation = gen + 1

            # 1. Evaluate fitness
            for genome in self._population:
                genome.fitness = fitness_fn(genome.parameters)
                total_evaluations += 1

            # 2. Sort by fitness (descending)
            self._population.sort(key=lambda g: g.fitness, reverse=True)

            # Record stats
            best_fit = self._population[0].fitness
            avg_fit = sum(g.fitness for g in self._population) / len(self._population)
            self._fitness_history.append(best_fit)
            self._avg_fitness_history.append(avg_fit)

            # Track best genome of the best param for convergence
            for name in self._parameter_ranges:
                parameter_convergence[name].append(self._population[0].parameters[name])

            # Update global best
            if self._best_genome is None or best_fit > self._best_genome.fitness:
                self._best_genome = Genome(
                    genome_id=self._population[0].genome_id,
                    parameters=dict(self._population[0].parameters),
                    fitness=self._population[0].fitness,
                    generation=self._population[0].generation,
                    parent_ids=list(self._population[0].parent_ids),
                )

            # Progress callback
            if progress_callback is not None:
                progress_callback(self._generation, best_fit, avg_fit)

            # 3-4. Build next generation
            next_gen: list[Genome] = []

            # Elites (deep copy)
            for elite in self._population[: self._elite_count]:
                next_gen.append(
                    Genome(
                        genome_id=elite.genome_id,
                        parameters=dict(elite.parameters),
                        fitness=elite.fitness,
                        generation=self._generation,
                        parent_ids=list(elite.parent_ids),
                    )
                )

            # Fill the rest
            while len(next_gen) < self._population_size:
                parent_a = self._select_parent()
                parent_b = self._select_parent()

                if self._rng.random() < self._crossover_rate:
                    child = self._crossover(parent_a, parent_b)
                else:
                    # Clone parent_a
                    child = Genome(
                        genome_id=f"g_{uuid.UUID(int=self._rng.getrandbits(128)).hex[:8]}",
                        parameters=dict(parent_a.parameters),
                        generation=self._generation,
                        parent_ids=[parent_a.genome_id],
                    )

                child = self._mutate(child)
                child.generation = self._generation
                next_gen.append(child)

            self._population = next_gen

        elapsed = time.monotonic() - t0

        assert self._best_genome is not None
        return EvolutionResult(
            generations_run=generations,
            population_size=self._population_size,
            best_genome=self._best_genome,
            best_fitness=self._best_genome.fitness,
            fitness_history=list(self._fitness_history),
            avg_fitness_history=list(self._avg_fitness_history),
            parameter_convergence=parameter_convergence,
            total_evaluations=total_evaluations,
            elapsed_seconds=elapsed,
        )

    def get_population(self) -> list[Genome]:
        """Current population sorted by fitness."""
        return sorted(self._population, key=lambda g: g.fitness, reverse=True)

    def get_best(self) -> Genome | None:
        return self._best_genome

    def get_stats(self) -> dict:
        return {
            "generation": self._generation,
            "population_size": self._population_size,
            "best_fitness": self._best_genome.fitness if self._best_genome else None,
            "best_genome_id": self._best_genome.genome_id if self._best_genome else None,
            "fitness_history": list(self._fitness_history),
            "avg_fitness_history": list(self._avg_fitness_history),
        }

    # ------------------------------------------------------------------
    # Genetic operators (private)
    # ------------------------------------------------------------------

    def _select_parent(self) -> Genome:
        """Tournament selection: pick tournament_size random, return best."""
        candidates = self._rng.sample(
            self._population, min(self._tournament_size, len(self._population))
        )
        return max(candidates, key=lambda g: g.fitness)

    def _crossover(self, parent_a: Genome, parent_b: Genome) -> Genome:
        """Uniform crossover: each param randomly from A or B."""
        child_params: dict[str, float] = {}
        for name in self._parameter_ranges:
            if self._rng.random() < 0.5:
                child_params[name] = parent_a.parameters[name]
            else:
                child_params[name] = parent_b.parameters[name]

        return Genome(
            genome_id=f"g_{uuid.UUID(int=self._rng.getrandbits(128)).hex[:8]}",
            parameters=child_params,
            generation=self._generation,
            parent_ids=[parent_a.genome_id, parent_b.genome_id],
        )

    def _mutate(self, genome: Genome) -> Genome:
        """Gaussian mutation: for each param with probability mutation_rate,
        add N(0, sigma * range_width).
        """
        params = dict(genome.parameters)
        for name, (lo, hi) in self._parameter_ranges.items():
            if self._rng.random() < self._mutation_rate:
                range_width = hi - lo
                perturbation = self._rng.gauss(0, self._mutation_sigma * range_width)
                params[name] = params[name] + perturbation
        params = self._clamp_parameters(params)

        return Genome(
            genome_id=genome.genome_id,
            parameters=params,
            fitness=genome.fitness,
            generation=genome.generation,
            parent_ids=list(genome.parent_ids),
        )

    def _clamp_parameters(self, params: dict[str, float]) -> dict[str, float]:
        """Clamp all parameters to their defined ranges."""
        clamped: dict[str, float] = {}
        for name, value in params.items():
            lo, hi = self._parameter_ranges[name]
            clamped[name] = max(lo, min(hi, value))
        return clamped
