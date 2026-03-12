"""Tests for Strategy Evolution genetic algorithm."""

import pytest

from bot.agents.strategy_evolution import EvolutionResult, Genome, StrategyEvolver


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

PARAM_RANGES = {
    "ema_fast": (5.0, 50.0),
    "ema_slow": (20.0, 200.0),
    "grid_spacing": (0.5, 5.0),
    "risk_pct": (0.5, 5.0),
    "stop_loss_atr": (1.0, 4.0),
}


def sphere_fitness(params: dict[str, float]) -> float:
    """Sphere function (minimize sum of squares). Optimal at all zeros.
    We negate because evolver maximizes.
    """
    return -sum(v ** 2 for v in params.values())


def _make_evolver(**kwargs) -> StrategyEvolver:
    defaults = dict(
        parameter_ranges=PARAM_RANGES,
        population_size=30,
        elite_count=3,
        seed=42,
    )
    defaults.update(kwargs)
    return StrategyEvolver(**defaults)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestStrategyEvolution:
    def test_initialize_population(self):
        """Correct size, all params within ranges."""
        evolver = _make_evolver()
        pop = evolver.initialize_population()

        assert len(pop) == 30
        for genome in pop:
            for name, (lo, hi) in PARAM_RANGES.items():
                assert lo <= genome.parameters[name] <= hi, (
                    f"{name}={genome.parameters[name]} outside [{lo}, {hi}]"
                )

    def test_evolve_improves_fitness(self):
        """After 10 generations, best_fitness > initial best."""
        evolver = _make_evolver()
        pop = evolver.initialize_population()

        # Evaluate initial fitness
        initial_best = max(sphere_fitness(g.parameters) for g in pop)

        result = evolver.evolve(sphere_fitness, generations=10)
        assert result.best_fitness > initial_best

    def test_evolve_result_structure(self):
        """EvolutionResult has all expected fields."""
        evolver = _make_evolver(population_size=10)
        result = evolver.evolve(sphere_fitness, generations=5)

        assert isinstance(result, EvolutionResult)
        assert result.generations_run == 5
        assert result.population_size == 10
        assert isinstance(result.best_genome, Genome)
        assert isinstance(result.best_fitness, float)
        assert isinstance(result.fitness_history, list)
        assert isinstance(result.avg_fitness_history, list)
        assert isinstance(result.parameter_convergence, dict)
        assert isinstance(result.total_evaluations, int)
        assert isinstance(result.elapsed_seconds, float)
        assert result.elapsed_seconds >= 0

    def test_elite_preserved(self):
        """Top elite_count genomes survive unchanged into the next generation's population."""
        elite_count = 2
        evolver = _make_evolver(population_size=10, elite_count=elite_count)
        evolver.initialize_population()

        # Manually evaluate and sort to know who the elites are
        for g in evolver._population:
            g.fitness = sphere_fitness(g.parameters)
        evolver._population.sort(key=lambda g: g.fitness, reverse=True)

        elite_ids = {evolver._population[i].genome_id for i in range(elite_count)}
        elite_params = {
            evolver._population[i].genome_id: dict(evolver._population[i].parameters)
            for i in range(elite_count)
        }

        # Run 1 generation — elites should be copied into the new population
        evolver.evolve(sphere_fitness, generations=1)
        pop_ids = {g.genome_id for g in evolver._population}

        assert elite_ids.issubset(pop_ids), (
            f"Elite IDs {elite_ids} not found in next generation {pop_ids}"
        )

        # Verify elite parameters are unchanged
        for g in evolver._population:
            if g.genome_id in elite_params:
                assert g.parameters == elite_params[g.genome_id]

    def test_parameters_within_ranges(self):
        """After evolution, all params still within ranges."""
        evolver = _make_evolver()
        result = evolver.evolve(sphere_fitness, generations=10)

        for genome in evolver.get_population():
            for name, (lo, hi) in PARAM_RANGES.items():
                assert lo <= genome.parameters[name] <= hi, (
                    f"{name}={genome.parameters[name]} outside [{lo}, {hi}]"
                )

        # Also check best genome
        for name, (lo, hi) in PARAM_RANGES.items():
            val = result.best_genome.parameters[name]
            assert lo <= val <= hi

    def test_mutation_changes_params(self):
        """Mutated genome differs from original."""
        evolver = _make_evolver(mutation_rate=1.0, mutation_sigma=0.5)
        evolver.initialize_population()

        original = evolver.get_population()[0]
        original_params = dict(original.parameters)

        mutated = evolver._mutate(original)

        # With mutation_rate=1.0 and high sigma, at least one param should differ
        changed = any(
            mutated.parameters[k] != original_params[k]
            for k in PARAM_RANGES
        )
        assert changed, "Mutation with rate=1.0 should change at least one parameter"

    def test_crossover_combines_parents(self):
        """Child has mix of both parents' params."""
        evolver = _make_evolver()
        evolver.initialize_population()

        parent_a = evolver.get_population()[0]
        parent_b = evolver.get_population()[1]

        # Run crossover many times to check it picks from both parents
        from_a = set()
        from_b = set()
        for _ in range(50):
            child = evolver._crossover(parent_a, parent_b)
            for name in PARAM_RANGES:
                if child.parameters[name] == parent_a.parameters[name]:
                    from_a.add(name)
                if child.parameters[name] == parent_b.parameters[name]:
                    from_b.add(name)

        # Over 50 trials, should have picked from both parents
        assert len(from_a) > 0, "Crossover never picked from parent A"
        assert len(from_b) > 0, "Crossover never picked from parent B"

    def test_tournament_selection(self):
        """Selected genome is from population."""
        evolver = _make_evolver()
        evolver.initialize_population()

        # Give them fitness values
        for g in evolver._population:
            g.fitness = sphere_fitness(g.parameters)

        selected = evolver._select_parent()
        pop_ids = {g.genome_id for g in evolver._population}
        assert selected.genome_id in pop_ids

    def test_fitness_history_length(self):
        """fitness_history has `generations` entries."""
        evolver = _make_evolver(population_size=10)
        result = evolver.evolve(sphere_fitness, generations=7)

        assert len(result.fitness_history) == 7
        assert len(result.avg_fitness_history) == 7

    def test_deterministic_with_seed(self):
        """Same seed produces same result."""
        evolver1 = _make_evolver(seed=123, population_size=10)
        result1 = evolver1.evolve(sphere_fitness, generations=5)

        evolver2 = _make_evolver(seed=123, population_size=10)
        result2 = evolver2.evolve(sphere_fitness, generations=5)

        assert result1.best_fitness == result2.best_fitness
        assert result1.best_genome.parameters == result2.best_genome.parameters
        assert result1.fitness_history == result2.fitness_history

    def test_genome_to_dict(self):
        """Genome serialization."""
        genome = Genome(
            genome_id="g_test1234",
            parameters={"ema_fast": 10.0, "ema_slow": 50.0},
            fitness=1.5,
            generation=3,
            parent_ids=["g_parent_a", "g_parent_b"],
        )
        d = genome.to_dict()

        assert d["genome_id"] == "g_test1234"
        assert d["parameters"] == {"ema_fast": 10.0, "ema_slow": 50.0}
        assert d["fitness"] == 1.5
        assert d["generation"] == 3
        assert d["parent_ids"] == ["g_parent_a", "g_parent_b"]

    def test_get_best(self):
        """Returns highest fitness genome."""
        evolver = _make_evolver(population_size=10)
        assert evolver.get_best() is None  # before evolution

        result = evolver.evolve(sphere_fitness, generations=5)
        best = evolver.get_best()

        assert best is not None
        assert best.fitness == result.best_fitness

    def test_convergence(self):
        """After 20 generations with sphere_fitness, params approach 0.

        The sphere function is minimized at origin. Since ranges don't include 0
        for all params (e.g. ema_fast min=5), the best should at least approach
        the boundary closest to 0.
        """
        evolver = _make_evolver(population_size=50, seed=42)
        result = evolver.evolve(sphere_fitness, generations=20)

        # For params whose range includes values near 0, they should get closer
        # For all params, the best should be at or near the lower bound
        best_params = result.best_genome.parameters
        for name, (lo, _hi) in PARAM_RANGES.items():
            # Best param should be within 50% of range from the lower bound
            range_width = _hi - lo
            assert best_params[name] <= lo + 0.5 * range_width, (
                f"{name}={best_params[name]} did not converge toward lower bound {lo}"
            )

        # Fitness should have improved over generations
        assert result.fitness_history[-1] > result.fitness_history[0]
