#!/usr/bin/env python3

import argparse
import random
import math
import string

HEX_CHARS = string.hexdigits.lower()[:16]


def clog2(n: int) -> int:
    return max(1, math.ceil(math.log2(n)))


def random_hex(length: int) -> str:
    return ''.join(random.choice(HEX_CHARS) for _ in range(length))


def main():
    parser = argparse.ArgumentParser(
        description="Random hex generator with column-scoped uniqueness"
    )
    parser.add_argument("--width", type=int, required=True)
    parser.add_argument("--depth", type=int, required=True)
    parser.add_argument("--columns", type=int, default=1)
    parser.add_argument("--unique", type=int)
    parser.add_argument(
        "--unique-columns",
        type=int,
        help="Number of columns to apply uniqueness to (randomly chosen)"
    )
    parser.add_argument("--seed", type=int, default=42,
                        help="Random seed (use same seed across runs to keep non-unique columns identical)")
    parser.add_argument("--output", type=str, default="output.txt")

    args = parser.parse_args()

    width = args.width
    depth = args.depth
    columns = args.columns
    unique = args.unique if args.unique is not None else depth
    unique_columns = (
        args.unique_columns if args.unique_columns is not None else columns
    )
    seed = args.seed

    if width % columns != 0:
        raise ValueError("Width must be evenly divisible by number of columns")

    if unique_columns > columns:
        raise ValueError("unique-columns cannot exceed total columns")

    col_width = width // columns
    repeats_per_unique = depth // unique

    # Separate RNG for meta decisions (which columns get uniqueness)
    meta_rng = random.Random(seed + 1000)
    unique_col_indices = set(meta_rng.sample(range(columns), unique_columns))

    column_data = []

    for col in range(columns):
        # Per-column RNG seeded by (seed + col). Non-unique columns produce
        # identical data across runs with the same seed.
        col_rng = random.Random(seed + col)

        if col in unique_col_indices and unique < depth:
            uniques = [''.join(col_rng.choice(HEX_CHARS) for _ in range(col_width))
                       for _ in range(unique)]
            expanded = []
            for u in uniques:
                expanded.extend([u] * repeats_per_unique)
            expanded = expanded[:depth]
            col_rng.shuffle(expanded)
        else:
            expanded = [''.join(col_rng.choice(HEX_CHARS) for _ in range(col_width))
                        for _ in range(depth)]

        column_data.append(expanded)

    id_width = clog2(depth+1)

    with open(args.output, "w") as f:
        for row in range(depth):
            row_hex = ''.join(column_data[col][row] for col in range(columns))
            verilog_id = f"{id_width}'d{row + 1}"
            f.write(f"{row_hex} {verilog_id}\n")


if __name__ == "__main__":
    main()