# Solver / library-driven PIR formulation vs the hand-written optimizer

Reproduce everything (from `engine/`; needs the venv with `dd`, `pyeda`):

```
for b in mixed_hi mixed_hi_overlap abc_anywhere non_contiguous abc_1byte; do
  pir_opt/.venv/bin/python -m pir_opt.solver.compare pir_opt/bench/$b.hex pir_opt/bench/$b.json --detail
done
pir_opt/.venv/bin/python -m pytest pir_opt -q          # 63 tests; system python3 -m pytest pir_opt -> 51 pass, solver tests skipped
```
(`python3 -m pir_opt.bench.make_synthetic` regenerates the three synthetic benches, written out from `tests/test_plan.py`.
Runs take 1-15 s per bench. Hard caps: MDD node cap, Espresso cube/literal cap, path cap, 20-30 s `SIGALRM` guards on the
pure-Python steps; a cap hit is printed as `skipped(too large)` / `NO (size cap)`.)

## 1. Formulation (what was implemented, `solver/`)

* **Lane value classes** (`classes.py`). For each lane every `(mask, value)` predicate used at that lane in ANY flit is
  collected (comparators are shared across time) and the 256 byte values are partitioned by predicate signature. Class 0 is
  the all-false class (`rest`, or a synthetic `absent` if the predicates cover all 256 values). **Short packets need no
  length variable:** golden is byte-granular and a constrained byte at/after the end never matches, i.e. "all predicates
  false" == class 0; absent bytes are simply class 0. Indicator `z(lane,c)`, c>=1, is a valid-gated comparator output;
  class 0 is the NOR of the others. Example (overlap bench lane 0, predicates 68/69/6x): `rest | 68 | 69 | 6x*` (`*` = 6x minus 68,69).
* **Time-ordered decision diagram** (`tdd.py`). Variables = one per byte `(t, lane)` ordered by `t` then lane, value = class.
  `F(x)` = id of the first matching row (priority fold over rows with an ITE chain on atom-class sets).
  Implementations: (a) pure-Python hash-consed multi-valued, multi-terminal DD (`MDD`) = the explicit fallback and the
  structure the machine is derived from; (b) `dd.autoref` BDD with binary class bits and `F(x,y)=(y==path_id(x))`, y at the bottom.
  Unused class codes map to class 0.
* **States per flit boundary** = distinct cofactors crossing the cut after flit `t` (Myhill-Nerode states of the Moore machine
  that reads one flit per cycle and outputs `F(prefix, rest absent)`). Both DDs are counted; they agree on every bench
  (tested). The last column is the number of distinct final outputs.
* **Derived machine** (`machine.py`). `g_t` = paths through flit `t`'s lane variables from every state (cubes of class sets) to
  the next state; output after flit `t` = state -> id. A lane class set becomes literals over indicators (`0 in C` -> AND of
  `~z_c` for the excluded classes, else OR of `z_c`), which exploits class exclusivity by construction. Encodings: **binary**
  (min bits, persistent states keep codes, the all-zero state is chosen greedily by Espresso literals), **one-hot**
  (strict, one bit per state), **1hot-0** (one-hot with one implicit all-zero state; extra variant). Every next-state bit and
  id bit is a single-output cover minimized by pyeda Espresso. No unused-code don't-cares are passed.
* **Simulation**: (i) explicit DD walk, (ii) evaluation of the *minimized* covers (validates cubes + encoding + Espresso),
  against golden on directed + 3000 random vectors incl. short and over-long packets: **0 mismatches in every column of every bench**.
* **Folding detection** (`fold_relation`): `g_t1` vs `g_t2` compared up to renaming of source and destination states
  (`iso`), or as a restriction (`embeds`, injective renaming).

## 2. Metric definitions (identical for all columns, `solver/compare.py` docstring)

Comparators: baseline one per atom, hand plan one per `cmp_l*` unit, solver the class indicators that survive in the minimized
logic (input bits = bits the indicator truly depends on; also given in *predicate form* = smallest predicate comparator set
from which all used indicators can be derived). State bits after flit `t` = information-carrying register bits (a bit still at
its reset constant is not counted). FFs allocated = registers instantiated (solver: one register reused across `t` = max over `t`).
Per-cycle logic = Espresso SOP literals of every live next-state bit as a function of that cycle's comparator outputs and state
bits; a hold (`bit' = bit`) costs 1 literal for everybody; flit-counter/enable decode is not counted for anyone.
For baseline/hand plan the functions are written from their structure (latch = one AND term, alive = one AND term incl. its
own bit, sticky = own bit OR consumer ANDs, carry = 1 literal) and pushed through the same Espresso; no truth-table enumeration
was needed. EOP output logic after flit `t` = Espresso SOP of the id bits over the live state bits (baseline/plan: the priority chain
with adjacent equal ids OR-merged and unwritten latches / `reach` folded as constants; solver: state -> id). Output logic
is Moore (reads the post-update state), the same convention as the hand plan's "EOP reads next-state values".

## 3. Tables

### mixed_hi
```
== mixed_hi: 64-bit flits (8 B), window 48 B, T=6
                                         baseline              hand plan         solver(binary)     solver(one-hot)         solver(1hot-0)     
-----------------------------------------------------------------------------------------------------------------------------------------------
distinct comparators                     52                    22                22                 22                      22                 
comparator input bits                    416                   176               176                176                     176                
  predicate-form comparators             52                    22                22                 22                      22                 
  predicate-form input bits              416                   176               176                176                     176                
state bits after flit t                  [0,3,5,13,22,30]      [0,3,3,5,5,4]     [0,2,2,3,3,3]      [0,3,4,6,6,5]           [0,2,3,5,5,4]      
FFs allocated                            30                    5                 3                  6                       5                  
comparators read per cycle               [0,4,2,15,16,15]      [0,4,2,15,16,15]  [0,4,2,15,16,15]   [0,4,2,15,16,15]        [0,4,2,15,16,15]   
next-state literals per cycle            [0,6,5,20,29,37]      [0,6,5,18,21,20]  [0,4,11,54,67,43]  [0,10,9,949,2272,1211]  [0,4,8,402,1175,62]
next-state literals total                97                    70                179                4451                    1651               
next-state terms total                   73                    42                57                 546                     209                
EOP output literals per t                [0,2,16,128,260,392]  [0,2,7,11,11,11]  [0,4,5,11,11,7]    [0,2,4,5,5,5]           [0,2,5,5,5,5]      
EOP priority-chain entries (structural)  26                    4                 0                  0                       0                  
EOP output literals max                  392                   11                11                 5                       5                  
mismatches vs golden (3190)              0                     0                 0                  0                       0                  
all covers Espresso-minimized            yes                   yes               yes                yes                     yes                

states at the cut after flit t (Moore, one flit per cycle; last column = final / output states)
  t              0    1    2    3    4    5
  MDD states     1    3    4    6    6    5
  BDD states     1    3    4    6    6    5
  min bits       0    2    2    3    3    3   BDD == MDD: yes

DD sizes: mdd_nodes=55, x_vars=48, bdd_nodes=137, bdd_vars=118
lane classes (class 0 = rest/absent):
  lane 0: rest | 68 | 69
  lane 1: rest | 68 | 69
  lane 2: rest | 68 | 69
  lane 3: rest | 68 | 69
  lane 4: rest | 08 | 68 | 69 | 86
  lane 5: rest | 00 | 68 | 69 | dd
  lane 6: rest | 68 | 69
  lane 7: rest | 06 | 11 | 68 | 69
```
### mixed_hi_overlap
```
== mixed_hi_overlap: 64-bit flits (8 B), window 48 B, T=6
                                         baseline            hand plan         solver(binary)          solver(one-hot)           solver(1hot-0)       
------------------------------------------------------------------------------------------------------------------------------------------------------
distinct comparators                     57                  27                27                      27                        27                   
comparator input bits                    452                 212               215                     215                       215                  
  predicate-form comparators             57                  27                27                      27                        27                   
  predicate-form input bits              452                 212               212                     212                       212                  
state bits after flit t                  [0,3,5,15,26,35]    [0,3,3,5,5,4]     [0,2,2,4,3,3]           [0,3,4,11,7,5]            [0,2,3,10,6,4]       
FFs allocated                            35                  5                 4                       11                        10                   
comparators read per cycle               [0,4,2,15,20,16]    [0,4,2,15,20,16]  [0,4,2,15,20,16]        [0,4,2,15,20,16]          [0,4,2,15,20,16]     
next-state literals per cycle            [0,6,5,23,35,42]    [0,6,5,21,25,21]  [0,4,11,2042,5277,103]  [0,10,9,4606,10395,1592]  [0,4,5,3174,5652,120]
next-state literals total                111                 78                7437                    16612                     8955                 
next-state terms total                   84                  42                805                     1987                      1121                 
EOP output literals per t                [0,0,0,7,570,3430]  [0,0,0,1,6,11]    [0,0,0,10,8,11]         [0,0,0,4,4,5]             [0,0,0,4,4,5]        
EOP priority-chain entries (structural)  26                  4                 0                       0                         0                    
EOP output literals max                  3430                11                11                      5                         5                    
mismatches vs golden (3190)              0                   0                 0                       0                         0                    
all covers Espresso-minimized            yes                 yes               yes                     yes                       yes                  

states at the cut after flit t (Moore, one flit per cycle; last column = final / output states)
  t              0    1    2    3    4    5
  MDD states     1    3    4   11    7    5
  BDD states     1    3    4   11    7    5
  min bits       0    2    2    4    3    3   BDD == MDD: yes

DD sizes: mdd_nodes=139, x_vars=48, bdd_nodes=330, bdd_vars=118
lane classes (class 0 = rest/absent):
  lane 0: rest | 6x* | 68 | 69
  lane 1: rest | 68 | 69
  lane 2: rest | 00 | 68 | 69
  lane 3: rest | 50 | 68 | 69
  lane 4: rest | 00 | 08 | 68 | 69 | 86
  lane 5: rest | 00 | 35 | 68 | 69 | dd
  lane 6: rest | 68 | 69
  lane 7: rest | 06 | 11 | 68 | 69
```
### abc_anywhere (abc anywhere + 3-flit anchored row)
```
== abc_anywhere: 64-bit flits (8 B), window 32 B, T=4
                                         baseline         hand plan      solver(binary)       solver(one-hot)          solver(1hot-0)    
-----------------------------------------------------------------------------------------------------------------------------------------
distinct comparators                     93               27             27                   27                       27                
comparator input bits                    744              216            216                  216                      216               
  predicate-form comparators             93               27             27                   27                       27                
  predicate-form input bits              744              216            216                  216                      216               
state bits after flit t                  [9,20,31,39]     [5,5,5,2]      [3,3,3,2]            [8,8,5,3]                [7,7,4,2]         
FFs allocated                            39               5              3                    8                        7                 
comparators read per cycle               [22,25,25,21]    [22,25,25,21]  [22,25,25,21]        [22,25,25,21]            [22,25,25,21]     
next-state literals per cycle            [22,34,45,52]    [22,30,30,26]  [40,51927,45597,35]  [1593,11981,11018,1463]  [918,6208,5518,79]
next-state literals total                153              108            97599                26055                    12723             
next-state terms total                   99               46             8006                 3524                     1815              
EOP output literals per t                [12,32,173,237]  [2,2,4,4]      [4,4,9,6]            [4,4,3,3]                [4,4,3,3]         
EOP priority-chain entries (structural)  31               2              0                    0                        0                 
EOP output literals max                  237              4              9                    4                        4                 
mismatches vs golden (3220)              0                0              0                    0                        0                 
all covers Espresso-minimized            yes              yes            NO (size cap)        yes                      yes               

states at the cut after flit t (Moore, one flit per cycle; last column = final / output states)
  t              0    1    2    3
  MDD states     8    8    5    3
  BDD states     8    8    5    3
  min bits       3    3    3    2   BDD == MDD: yes

DD sizes: mdd_nodes=144, x_vars=32, bdd_nodes=344, bdd_vars=80
lane classes (class 0 = rest/absent):
  lane 0: rest | 61 | 62 | 63
  lane 1: rest | 61 | 62 | 63
  lane 2: rest | 61 | 62 | 63
  lane 3: rest | 61 | 62 | 63 | aa
  lane 4: rest | 61 | 62 | 63 | bb
  lane 5: rest | 61 | 62 | 63 | cc
  lane 6: rest | 61 | 62 | 63
  lane 7: rest | 61 | 62 | 63
```
`NO (size cap)`: solver(binary) cover for flits 1-2 exceeded the 3000-cube Espresso cap; the printed literal numbers for
that column are the UNMINIMIZED path covers (upper bound), not Espresso results.
### non_contiguous (non-contiguous priority)
```
== non_contiguous: 64-bit flits (8 B), window 16 B, T=2
                                         baseline  hand plan  solver(binary)  solver(one-hot)  solver(1hot-0)
-------------------------------------------------------------------------------------------------------------
distinct comparators                     8         8          8               8                8             
comparator input bits                    64        64         64              64               64            
  predicate-form comparators             8         8          8               8                8             
  predicate-form input bits              64        64         64              64               64            
state bits after flit t                  [3,5]     [3,3]      [2,2]           [3,3]            [2,2]         
FFs allocated                            5         3          2               3                2             
comparators read per cycle               [4,4]     [4,4]      [4,4]           [4,4]            [4,4]         
next-state literals per cycle            [5,7]     [5,7]      [6,10]          [11,20]          [7,8]         
next-state literals total                12        12         16              31               15            
next-state terms total                   8         8          7               13               7             
EOP output literals per t                [7,9]     [7,7]      [5,8]           [4,4]            [4,4]         
EOP priority-chain entries (structural)  5         3          0               0                0             
EOP output literals max                  9         7          8               4                4             
mismatches vs golden (3064)              0         0          0               0                0             
all covers Espresso-minimized            yes       yes        yes             yes              yes           

states at the cut after flit t (Moore, one flit per cycle; last column = final / output states)
  t              0    1
  MDD states     3    3
  BDD states     3    3
  min bits       2    2   BDD == MDD: yes

DD sizes: mdd_nodes=11, x_vars=12, bdd_nodes=23, bdd_vars=20
lane classes (class 0 = rest/absent):
  lane 0: rest
  lane 1: rest | 68
  lane 2: rest | 68 | 69
  lane 3: rest | 68 | 69
  lane 4: rest | 69
  lane 5: rest | 68
  lane 6: rest | 69
  lane 7: rest
```
### abc_1byte (extra: 1-byte flits, T=16, carry chains of depth 2)
```
== abc_1byte: 8-bit flits (1 B), window 16 B, T=16
                                         baseline                                                                                                                                                                      hand plan                          solver(binary)                                 solver(one-hot)                                  solver(1hot-0)                              
------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
distinct comparators                     42                                                                                                                                                                            3                                  3                                              3                                                3                                           
comparator input bits                    336                                                                                                                                                                           24                                 24                                             24                                               24                                          
  predicate-form comparators             42                                                                                                                                                                            3                                  3                                              3                                                3                                           
  predicate-form input bits              336                                                                                                                                                                           24                                 24                                             24                                               24                                          
state bits after flit t                  [1,3,6,9,12,15,18,21,24,27,30,33,36,39,41,42]                                                                                                                                 [1,3,4,4,4,4,4,4,4,4,4,4,4,4,3,1]  [1,2,2,2,2,2,2,2,2,2,2,2,2,2,2,1]              [2,3,4,4,4,4,4,4,4,4,4,4,4,4,3,2]                [1,2,3,3,3,3,3,3,3,3,3,3,3,3,2,1]           
FFs allocated                            42                                                                                                                                                                            4                                  2                                              4                                                3                                           
comparators read per cycle               [1,2,3,3,3,3,3,3,3,3,3,3,3,3,2,1]                                                                                                                                             [1,2,3,3,3,3,3,3,3,3,3,3,3,3,2,1]  [1,2,3,3,3,3,3,3,3,3,3,3,3,3,2,1]              [1,2,3,3,3,3,3,3,3,3,3,3,3,3,2,1]                [1,2,3,3,3,3,3,3,3,3,3,3,3,3,2,1]           
next-state literals per cycle            [1,3,6,9,12,15,18,21,24,27,30,33,36,39,41,42]                                                                                                                                 [1,3,6,7,7,7,7,7,7,7,7,7,7,7,6,4]  [1,3,13,11,11,11,11,11,11,11,11,11,11,11,7,4]  [2,11,18,19,19,19,19,19,19,19,19,19,19,19,10,6]  [1,3,5,11,11,11,11,11,11,11,11,11,11,11,5,3]
next-state literals total                357                                                                                                                                                                           97                                 149                                            256                                              138                                         
next-state terms total                   357                                                                                                                                                                           69                                 81                                             124                                              77                                          
EOP output literals per t                [0,0,3,6,9,12,15,18,skipped(too large),skipped(too large),skipped(too large),skipped(too large),skipped(too large),skipped(too large),skipped(too large),skipped(too large)]  [0,0,1,1,1,1,1,1,1,1,1,1,1,1,1,1]  [0,0,2,2,2,2,2,2,2,2,2,2,2,2,2,1]              [0,0,1,1,1,1,1,1,1,1,1,1,1,1,1,1]                [0,0,1,1,1,1,1,1,1,1,1,1,1,1,1,1]           
EOP priority-chain entries (structural)  14                                                                                                                                                                            1                                  0                                              0                                                0                                           
EOP output literals max                  18                                                                                                                                                                            1                                  2                                              1                                                1                                           
mismatches vs golden (3116)              0                                                                                                                                                                             0                                  0                                              0                                                0                                           
all covers Espresso-minimized            NO (size cap)                                                                                                                                                                 yes                                yes                                            yes                                              yes                                         

states at the cut after flit t (Moore, one flit per cycle; last column = final / output states)
  t              0    1    2    3    4    5    6    7    8    9   10   11   12   13   14   15
  MDD states     2    3    4    4    4    4    4    4    4    4    4    4    4    4    3    2
  BDD states     2    3    4    4    4    4    4    4    4    4    4    4    4    4    3    2
  min bits       1    2    2    2    2    2    2    2    2    2    2    2    2    2    2    1   BDD == MDD: yes

DD sizes: mdd_nodes=44, x_vars=16, bdd_nodes=92, bdd_vars=36
lane classes (class 0 = rest/absent):
  lane 0: rest | 61 | 62 | 63
```

## 4. Comparators read per cycle (mixed_hi_overlap; mixed_hi is the same without the nibble/port bytes)

All three approaches read exactly the same comparator sets per cycle on every bench (the counts in the tables: mixed_hi
`[0,4,2,15,16,15]`). Baseline names are per `(t,lane,pred)`; the hand plan and the solver share them across time.
```
  hand plan:
    t=1: cmp_l4_08 cmp_l4_86 cmp_l5_00 cmp_l5_dd
    t=2: cmp_l7_06 cmp_l7_11
    t=3: cmp_l0_68 cmp_l1_68 cmp_l1_69 cmp_l2_68 cmp_l2_69 cmp_l3_68 cmp_l3_69 cmp_l4_68 cmp_l4_69 cmp_l5_68 cmp_l5_69 cmp_l6_68 cmp_l6_69 cmp_l7_68 cmp_l7_69
    t=4: cmp_l0_68 cmp_l0_69 cmp_l1_68 cmp_l1_69 cmp_l2_00 cmp_l2_68 cmp_l2_69 cmp_l3_50 cmp_l3_68 cmp_l3_69 cmp_l4_00 cmp_l4_68 cmp_l4_69 cmp_l5_35 cmp_l5_68 cmp_l5_69 cmp_l6_68 cmp_l6_69 cmp_l7_68 cmp_l7_69
    t=5: cmp_l0_68 cmp_l0_69 cmp_l0_6x cmp_l1_68 cmp_l1_69 cmp_l2_68 cmp_l2_69 cmp_l3_68 cmp_l3_69 cmp_l4_68 cmp_l4_69 cmp_l5_68 cmp_l5_69 cmp_l6_68 cmp_l6_69 cmp_l7_69
  solver(binary):
    t=1: l4:08 l4:86 l5:00 l5:dd
    t=2: l7:06 l7:11
    t=3: l0:68 l1:68 l1:69 l2:68 l2:69 l3:68 l3:69 l4:68 l4:69 l5:68 l5:69 l6:68 l6:69 l7:68 l7:69
    t=4: l0:68 l0:69 l1:68 l1:69 l2:00 l2:68 l2:69 l3:50 l3:68 l3:69 l4:00 l4:68 l4:69 l5:35 l5:68 l5:69 l6:68 l6:69 l7:68 l7:69
    t=5: l0:6x* l0:68 l0:69 l1:68 l1:69 l2:68 l2:69 l3:68 l3:69 l4:68 l4:69 l5:68 l5:69 l6:68 l6:69 l7:69
```
Overlap class form: the solver needs the carved class `6x*` (8 bits) where the plan uses the 4-bit `6x` comparator, hence
215 vs 212 input bits; in predicate form it is identical (27 comparators, 212 bits).

## 5. Folding detection
mixed_hi:
```
folding (g_t1 vs g_t2 up to state renaming; 'iso' = identical, 'embeds' = g_t1 is a restriction of g_t2):
  trivial cycles (every state ignores the flit): [0]
  identical up to renaming (iso classes of non-trivial cycles): none
  strict restrictions (g_x < g_y: g_x is a sub-machine of g_y): g3<g4
```
mixed_hi_overlap:
```
folding (g_t1 vs g_t2 up to state renaming; 'iso' = identical, 'embeds' = g_t1 is a restriction of g_t2):
  trivial cycles (every state ignores the flit): [0]
  identical up to renaming (iso classes of non-trivial cycles): none
  strict restrictions (g_x < g_y: g_x is a sub-machine of g_y): none
```
abc_anywhere / non_contiguous: none related. abc_1byte:
```
folding (g_t1 vs g_t2 up to state renaming; 'iso' = identical, 'embeds' = g_t1 is a restriction of g_t2):
  trivial cycles (every state ignores the flit): []
  identical up to renaming (iso classes of non-trivial cycles): {3,4,5,6,7,8,9,10,11,12,13}
  strict restrictions (g_x < g_y: g_x is a sub-machine of g_y): g0<g1, g0<g2, g0<g3, g0<g4, g0<g5, g0<g6, g0<g7, g0<g8, g0<g9, g0<g10, g0<g11, g0<g12, g0<g13, g1<g2, g1<g3, g1<g4 ...
```
The DD finds folding automatically where it exists: on 1-byte flits cycles 3..13 are the same machine (one shared consumer with an
enable = the hand plan's folded family), and on mixed_hi the flit-3 logic is a restriction of the flit-4 logic (flit 3 starts
in the "no carry" state): a shared `g` with an enable is possible, but the solver does not itself emit the shared structure.

## 6. Honest conclusion

**Where the solver wins**
* **State bits / FFs.** Min states per boundary are `[1,3,4,6,6,5]` on mixed_hi (the expected 6 after flit 3: three decided
  rows, found, not-found+carry, not-found). Binary: 3 FFs vs 5 (hand plan) vs 30 (baseline). Overlap: 4 vs 5 (the minimal machine
  needs 11 states after flit 3, but the register is shared). abc_anywhere 3 vs 5, non_contiguous 2 vs 3, abc_1byte 2 vs 4.
* **Derives the structure without heuristics.** It finds the same comparator set as the hand plan on every bench (22/27/27/8/3) and the
  same per-cycle comparator reads, as a by-product of cofactor equality (carry/shared comparators across time appear automatically).
  The DD also detects folding (iso/embedding) with no pattern knowledge; it handled the non-contiguous-priority bench correctly with no
  special case (priority is just the ITE order).
* **EOP logic** is a pure state->id decode (no priority chain): 4-11 literals binary, 4-5 one-hot (hand plan 4-11, baseline up to 3430
  as flattened SOP; the baseline's real structure is a 26-entry if/else chain).

**Where it loses**
* **Next-state logic is much bigger.** mixed_hi next-state literals: hand plan 70, baseline 97, solver binary 179, 1hot-0 1651,
  strict one-hot 4451; overlap 78 vs 7437 / 8955 / 16612. Reason: the minimal machine makes the states mutually exclusive
  (found / not-found+carry / not-found, decided ids). The hand plan keeps `found` and `carry` as independent bits (found' = found | OR(and),
  carry' = cmp), so nothing is ever negated; the exclusive encoding needs "no pair hit" for the carry/not-found targets, whose
  two-level SOP grows like a Fibonacci sequence in the number of lanes, and the DD flattens everything to two levels. More states
  would mean simpler logic (a non-minimal, factored state wins here) - the min-state objective and the min-logic objective disagree.
  Multi-level optimization (or Espresso with state-aliasing don't-cares such as "found, carry bit = don't care") is the missing step;
  none was implemented.
* **Scalability.** Path enumeration is the bottleneck, not the DD: abc_anywhere (8 states, 8844 paths in flit 1) already exceeds
  the Espresso cap for the binary column. The DD itself is tiny (11 to 144 MDD nodes, 23 to 344 BDD nodes) on every bench,
  never blew up. The 48-byte windows here are small; a 64 B flit x 24 flits design would need a path-free logic derivation.
* **Class indicators vs predicates**: exact-class comparators can need more input bits (6x* 8 bits vs 6x 4 bits); predicate form fixes it.
* The baseline's SOP-flattened EOP numbers (392, 3430) are an artifact of two-level SOP over a 26-row chain; they are not a
  realistic hardware cost, they only show the structure is not two-level friendly.

**Net**: as an analysis tool the solver is better than the heuristics (minimal state per boundary, automatic sharing/folding,
a proof-like cross check; it confirms the hand plan's comparator set is minimal and that the hand plan's 5 FFs could be 3). As a
logic generator it currently loses by 2.5x-100x in two-level literals. Recommended use: take the solver's state counts,
comparator sets and folding relations as a lower bound / oracle for the hand plan, not as the implementation.

## 7. Caveats
* States count includes impossible suffixes (e.g. short flit followed by more data) only where they are distinguishable by
  the total function F; absent == rest means this never adds states in these benches.
* The zero-state search and persistence heuristic for binary codes is greedy; better encodings may lower literals somewhat.
* No unused-code or exclusivity don't-cares are given to Espresso (exclusivity is built into the cubes; unused state codes are not exploited).
* Output logic after the final flit counts a separate decode although the last state could simply be the id.
* Slow step found on abc_1byte: baseline EOP priority chain (`logic.chain_cover`, complement distribution with an O(n^2) reduce), not the
  DD, cofactor counting, explicit fallback, or Espresso. Now capped (1500 cubes / 20 s) and printed as skipped when exceeded.
