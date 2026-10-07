# Archive — not part of the active flow

Parked experiments, kept for reference only. Nothing in the active `pir_opt` code imports these files.

- `solver/`, `SOLVER.md`, `test_solver.py`: a decision-diagram plus Espresso formulation, compared against the hand plan (2026-10-06).
  - It confirmed that the hand plan's comparator set is minimal.
  - Minimal state saved 1–2 FFs but made the next-state logic 2.6×–95× larger, so we dropped it.
  - Use it as a lower bound / oracle if needed.
  - It needs the venv in `../.venv` (`dd`, `pyeda`; see `requirements_solver.txt`).
  - The imports still say `pir_opt.solver`. Restore it to `pir_opt/solver` before running.
- `optimizers/`: a partial BLIF netlist export for ABC that was never finished, abandoned in favour of software-only metrics.
