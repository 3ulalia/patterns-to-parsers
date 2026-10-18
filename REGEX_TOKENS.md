# Regex custom tokens

## When you need one

The `.hex` mask already covers most patterns. Reach for `REGEX` only for the two things it can't express:

| what you want | how to write it |
|---|---|
| exact nibbles | mask literal — `ABCD` |
| any nibble, fixed position | mask wildcard — `x` |
| fixed-length gap | mask wildcards — `xxx` |
| field equals one of a set | `EQOR` token |
| field not equal to a value | `NE` token |
| field within a numeric range | `RANGE` token |
| **nibble drawn from a set** | **`REGEX` — class** |
| **variable-length gap, floating position, optional part** | **`REGEX` — repetition** |

Everything is nibbles. A term list never mentions bytes.

## Reserving space in the mask

The token `id` occupies its longest possible match, in nibbles. `.{0,4}ABCD` matches at most 8 nibbles, so the id is 8 characters:

```
.hex     xxxxGGGGGGGGxxxx   13'd1
```

Reserve the token over **only** the part the mask can't handle. Literals and fixed gaps stay in the mask, where they are free.

Ids use letters outside `A`–`F`, and not `X`.

## Terms

A pattern is a list of terms. Each term is a predicate and a repetition count. A term's span is `pred width × rep`.

## Predicates

| regex | JSON `pred` | width |
|---|---|---|
| `.` | `{"kind":"any"}` | 1 nibble |
| `[0-9]` | `{"kind":"class","ranges":[["0","9"]]}` | 1 nibble |
| `[0-27-9]` | `{"kind":"class","ranges":[["0","2"],["7","9"]]}` | 1 nibble |
| `[^0F]` | `{"kind":"class","ranges":[["0","0"],["F","F"]],"negate":true}` | 1 nibble |
| `ABCD` | `{"kind":"lit","value":"ABCD"}` | 4 nibbles |
| `(ABCD\|1234)` | `{"kind":"oneof","values":["ABCD","1234"]}` | 4 nibbles |

`lit` exists so a literal can sit *after* a variable-length term, where its position floats and the mask can't hold it. A literal at a fixed position belongs in the mask instead.

## Repetition

| regex | JSON `rep` |
|---|---|
| `a{3}` | `{"min":3,"max":3}` |
| `a?` | `{"min":0,"max":1}` |
| `a{2,5}` | `{"min":2,"max":5}` |
| `a*` | `{"min":0,"max":"W"}` |
| `a+` | `{"min":1,"max":"W"}` |

`W` is the window size in nibbles, from `max_width`. Unbounded repetition clamps to it.

Add `step` to restrict the slide — `{"min":0,"max":4,"step":2}` allows a shift of 0, 2 or 4 nibbles, keeping a pattern byte-aligned and halving the positions built.

## Anchoring

`at` pins a term to absolute nibble positions in the window:

```json
"at": {"min":0,"max":0}     // ^
"at": {"min":4,"max":8}     // somewhere in nibbles 4-8
```

## Alternation

Folds into a single term when the branches are the same width:

| regex | JSON `pred` |
|---|---|
| `(0\|5\|A)` | `{"kind":"class","ranges":[["0","0"],["5","5"],["A","A"]]}` |
| `(ABCD\|1234)` | `{"kind":"oneof","values":["ABCD","1234"]}` |

Unequal widths become two `.hex` rows sharing one id.

## Examples

**Two digit nibbles, then `0900`** — the only part the mask can't do is the digit class, so the token covers just those 2 nibbles:

```
.hex     ...xxGG0900...   13'd1
```
```json
{ "id": "GG", "type": "const", "opcode": "REGEX",
  "terms": [ {"pred": {"kind":"class","ranges":[["0","9"]]}, "rep": {"min":2,"max":2}} ] }
```

**`/.{0,4}ABCD/`** — `ABCD` floating within the first 4 nibbles. 8 nibbles reserved:

```json
{ "id": "HHHHHHHH", "type": "const", "opcode": "REGEX",
  "terms": [
    {"pred": {"kind":"any"},                    "rep": {"min":0,"max":4}},
    {"pred": {"kind":"lit","value":"ABCD"},     "rep": {"min":1,"max":1}}
  ] }
```

**`/[0-9]{1,6}FFFF/`** — a variable-length digit run followed by a marker. 10 nibbles reserved:

```json
{ "id": "JJJJJJJJJJ", "type": "const", "opcode": "REGEX",
  "terms": [
    {"pred": {"kind":"class","ranges":[["0","9"]]}, "rep": {"min":1,"max":6}},
    {"pred": {"kind":"lit","value":"FFFF"},         "rep": {"min":1,"max":1}}
  ] }
```

**`/(ABCD|1234).{0,8}99/`** — either marker, then `99` within 8 nibbles. 14 nibbles reserved:

```json
{ "id": "KKKKKKKKKKKKKK", "type": "const", "opcode": "REGEX",
  "terms": [
    {"pred": {"kind":"oneof","values":["ABCD","1234"]}, "rep": {"min":1,"max":1}},
    {"pred": {"kind":"any"},                            "rep": {"min":0,"max":8}},
    {"pred": {"kind":"lit","value":"99"},               "rep": {"min":1,"max":1}}
  ] }
```

## JSON reference

A `REGEX` token is one entry in the `custom` array of the config JSON.

### Entry

| key | required | meaning |
|---|---|---|
| `id` | yes | The placeholder string that appears in the `.hex` mask. Length in characters equals the pattern's longest match in nibbles. Uses letters outside `A`-`F`, and not `X`. |
| `type` | yes | Always `"const"`. |
| `opcode` | yes | Always `"REGEX"`. |
| `terms` | yes | Ordered list of terms. |

### Term

| key | required | meaning |
|---|---|---|
| `pred` | yes | What each repetition must match. |
| `rep` | no | How many times `pred` repeats. Defaults to `{"min":1,"max":1}`. |
| `at` | no | Constrains where this term may start, in absolute nibble positions. Unconstrained by default. |

### `pred`

| key | applies to | meaning |
|---|---|---|
| `kind` | all | One of `any`, `class`, `lit`, `oneof`. |
| `ranges` | `class` | List of `[lo, hi]` nibble pairs, inclusive. Single nibbles are written `["5","5"]`. |
| `negate` | `class` | Inverts the set. Defaults to `false`. |
| `value` | `lit` | Hex string to match exactly. |
| `values` | `oneof` | List of hex strings, any one of which matches. All must be the same width. |

Predicate width: `any` and `class` are 1 nibble; `lit` is the length of `value`; `oneof` is the common length of `values`.

### `rep`

| key | required | meaning |
|---|---|---|
| `min` | yes | Fewest repetitions. |
| `max` | yes | Most repetitions. `"W"` means the window size in nibbles, from `max_width`. |
| `step` | no | Allowed increments between `min` and `max`. Defaults to `1`; use `2` to keep a pattern byte-aligned. |

### `at`

| key | required | meaning |
|---|---|---|
| `min` | yes | Earliest nibble position the term may start at. |
| `max` | yes | Latest nibble position the term may start at. |

Positions are absolute within the match window. `{"min":0,"max":0}` is the equivalent of `^`.

## Cost

Each term is built once per position it can start at. A term list with no variable-length repetition has one position and costs the same as a plain mask row. Every `{m,n}` with `m ≠ n` multiplies the positions of everything after it, so keep `max` as tight as the pattern allows, and use `at` and `step` wherever you can.

## Not supported

Backreferences, recursion, and conditional groups are not regular and cannot be built. Variable-width lookbehind is not supported.

Greedy, lazy, possessive and atomic quantifiers parse but have no effect — a `REGEX` token reports only whether it matched, not where.
