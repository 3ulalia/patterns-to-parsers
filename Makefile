# =============================================================================
# PatternToParser — Push-button RTL generation and synthesis flows
#
# RTL generation (Python + jinja2; p4c only needed for the P4 flow):
#   make p4          — compile P4 parsers to SystemVerilog  →  p4_out/
#   make synthetic   — generate RTL from synthetic benchmarks →  synthetic_out/
#   make snort       — generate RTL from Snort rules          →  snort_out/
#   make all         — run all three flows
#
# Artifact Evaluation (requires Vivado / Quartus):
#   make table1      — reproduce Table 1 (Vivado):    RTL + synth + CSV
#   make table3      — reproduce Table 3 (both tools): RTL + synth + CSV
#   make fig6        — reproduce Figure 6 (Quartus):  RTL + synth + CSV
#   make fig7        — reproduce Figure 7 (Vivado):   RTL + synth + CSV
#   make fig9        — reproduce Figure 9 (Vivado):   RTL + synth + CSV
#   make fig-rtl     — RTL shared by fig6 & fig7 (no tool needed)
#   make table1-rtl  — Table 1 RTL only (no tool needed)
#   make table3-rtl  — Table 3 RTL only (no tool needed)
#   make fig9-rtl    — Figure 9 RTL only (no tool needed)
#
# Synthesis (requires Vivado or Quartus):
#   make synth   TOOL=vivado   DIR=p4_out/full_parser
#   make synth   TOOL=quartus  DIR=p4_out/full_parser
#   make extract TOOL=vivado   DIR=p4_out/full_parser
#
# Override tool paths if not on PATH:
#   make synth VIVADO=/path/to/vivado QUARTUS=/path/to/quartus_sh
#   make table1 VIVADO=/path/to/vivado
# =============================================================================

SHELL := /bin/bash

# ---------------------------------------------------------------------------
# RTL generation settings
# ---------------------------------------------------------------------------
DWS              := 64 128 256 320 512
FULL_PARSER_MW   := 896   # actual mw per dw = ceil(FULL_PARSER_MW/dw)*dw
SIMPLE_PARSER_MW := 768   # actual mw per dw = ceil(SIMPLE_PARSER_MW/dw)*dw

SNORT_INPUT := snort_frontend/snort_10_rule.txt
SNORT_WIDTH := 128  # max packet width in bytes

# ---------------------------------------------------------------------------
# Synthesis tool paths (override on the command line if not on PATH)
# ---------------------------------------------------------------------------
TOOL    ?= vivado
VIVADO  ?= vivado
QUARTUS ?= quartus_sh

VIV_TCL_SCRIPT ?= run_vivado.tcl
QUA_TCL_SCRIPT ?= run_quartus.tcl

DIR        ?= p4_out/full_parser
SUBDIRS    := $(filter-out $(DIR),$(wildcard $(DIR)/*))
DONE_FILES := $(addsuffix /.done,$(SUBDIRS))

# ---------------------------------------------------------------------------
# Artifact Evaluation — Table 1 configurations
# ---------------------------------------------------------------------------
TABLE1_DIR        := artifact_out/table1
TABLE1_XDC        := $(CURDIR)/vivado.xdc
TABLE1_RESULTS    := table1_results.csv

TABLE1_SIMPLE_DWS := 256 320
TABLE1_FULL_DWS   := 64 320 512
TABLE1_MODES      := tree unique

FIG_DIR     := artifact_out/fig
FIG_DWS     := 64 128 256 320 512
FIG6_CSV    := fig6_results.csv
FIG7_CSV    := fig7_results.csv

TABLE3_DESIGN := artifact_out/table3/snort_10rule
TABLE3_CSV    := table3_results.csv

FIG9_DIR      := artifact_out/fig9
FIG9_CSV      := fig9_results.csv

.PHONY: all p4 p4_full p4_simple synthetic snort \
        synth run extract clean clean-synth clean-done \
        table1 table1-rtl table1-synth table1-extract \
        fig6 fig6-synth fig6-extract \
        fig7 fig7-synth fig7-extract \
        fig-rtl \
        table3 table3-rtl table3-synth-vivado table3-synth-quartus table3-extract \
        fig9 fig9-rtl fig9-synth fig9-extract


# =============================================================================
# RTL GENERATION FLOWS
# =============================================================================

all: p4 synthetic snort

# --- Flow 1: P4 → RTL --------------------------------------------------------

p4: p4_full p4_simple

p4_full:
	@echo "================================================================"
	@echo " Flow 1a: Full Parser  (p4/full_parser.p4)  dw sweep: $(DWS)"
	@echo "================================================================"
	@for dw in $(DWS); do \
	    mw=$$(python3 -c "import math; print(math.ceil($(FULL_PARSER_MW)/$$dw)*$$dw)"); \
	    echo "  --> dw=$$dw  mw=$$mw"; \
	    python3 engine/p4_to_rtl.py \
	        --p4 p4/full_parser.p4 \
	        --dw $$dw --mw $$mw --cm 1 \
	        --o p4_out/full_parser/dw_$$dw || exit 1; \
	done
	@echo "Full parser RTL written to p4_out/full_parser/"

p4_simple:
	@echo "================================================================"
	@echo " Flow 1b: Simple Parser (p4/simple_parser.p4) dw sweep: $(DWS)"
	@echo "================================================================"
	@for dw in $(DWS); do \
	    mw=$$(python3 -c "import math; print(math.ceil($(SIMPLE_PARSER_MW)/$$dw)*$$dw)"); \
	    echo "  --> dw=$$dw  mw=$$mw"; \
	    python3 engine/p4_to_rtl.py \
	        --p4 p4/simple_parser.p4 \
	        --dw $$dw --mw $$mw --cm 1 \
	        --o p4_out/simple_parser/dw_$$dw || exit 1; \
	done
	@echo "Simple parser RTL written to p4_out/simple_parser/"

# --- Flow 2: Synthetic hex → RTL ---------------------------------------------

synthetic:
	@echo "================================================================"
	@echo " Flow 2: Synthetic benchmarks"
	@echo "================================================================"
	@for json in synthetic/one_group.json synthetic/multi_group.json; do \
	    for hex in synthetic/synthetic_*.hex; do \
	        hex_base=$$(basename $$hex .hex); \
	        json_base=$$(basename $$json .json); \
	        out=synthetic_out/$${json_base}_$${hex_base}; \
	        echo "  $$json_base + $$hex_base --> $$out"; \
	        PYTHONPATH=engine python3 engine/tools/build_rtl_countluts.py \
	            -c $$json -m $$hex \
	            -o $$out --rtl --tree1 --tree2 || exit 1; \
	    done; \
	done
	@echo "Synthetic RTL written to synthetic_out/"

# --- Flow 3: Snort rules → RTL -----------------------------------------------

snort:
	@echo "================================================================"
	@echo " Flow 3: Snort rules  ($(SNORT_INPUT))"
	@echo "================================================================"
	python3 snort_frontend/create_snort_config.py \
	    --i $(SNORT_INPUT) \
	    --w $(SNORT_WIDTH) \
	    --o snort_frontend/snort_out.hex
	@PYTHONPATH=engine python3 engine/tools/build_rtl_countluts.py \
	    -c snort_frontend/snort_out_config.json \
	    -m snort_frontend/snort_out.hex \
	    -o snort_out --rtl --tree1 --tree2
	@echo "Snort RTL written to snort_out/"


# =============================================================================
# SYNTHESIS FLOW  (Vivado / Quartus)
# =============================================================================

synth: run

run: $(DONE_FILES)

$(DIR)/%/.done:
	@subdir=$(DIR)/$*; \
	echo "=========================================="; \
	echo " Running $(TOOL) for $$subdir"; \
	echo "=========================================="; \
	if [ "$(TOOL)" = "vivado" ]; then \
	    $(VIVADO) -mode batch -source $(VIV_TCL_SCRIPT) -tclargs $(DIR) $$subdir || exit 1; \
	elif [ "$(TOOL)" = "quartus" ]; then \
	    $(QUARTUS) -t $(QUA_TCL_SCRIPT) $$subdir || exit 1; \
	    proj_name=$$(basename $$subdir)_proj; \
	    $(QUARTUS) --flow compile $$subdir/quartus_proj/$$proj_name || exit 1; \
	else \
	    echo "Error: Unknown TOOL=$(TOOL). Use vivado or quartus."; exit 1; \
	fi; \
	touch $$subdir/.done

extract:
	@echo "Extracting resources ($(TOOL)) from $(DIR)"
	@if [ "$(TOOL)" = "vivado" ]; then \
	    for proj in $$(find $(DIR) -maxdepth 2 -type d -name vivado_proj); do \
	        python3 vivado_extract_resources.py "$$proj"; \
	    done; \
	elif [ "$(TOOL)" = "quartus" ]; then \
	    for proj in $$(find $(DIR) -maxdepth 2 -type d -name quartus_proj); do \
	        python3 quartus_extract_resources.py "$$proj"; \
	    done; \
	else \
	    echo "Error: Unknown TOOL=$(TOOL)."; exit 1; \
	fi


# =============================================================================
# ARTIFACT EVALUATION — Table 1 (make table1)
#
# Reproduces Table 1 from the paper end-to-end:
#   1. Generate RTL for all 10 configurations
#   2. Synthesize each with Vivado
#   3. Extract results into table1_results.csv
#
# Prerequisites: Vivado on PATH (or set VIVADO=/path/to/vivado)
# Individual steps: table1-rtl | table1-synth | table1-extract
# =============================================================================

table1: table1-rtl table1-synth table1-extract

table1-rtl:
	@echo "================================================================"
	@echo " Table 1 — RTL Generation"
	@echo " Simple Parser: dw=$(TABLE1_SIMPLE_DWS)  Full Parser: dw=$(TABLE1_FULL_DWS)"
	@echo " Modes: $(TABLE1_MODES)"
	@echo "================================================================"
	@mkdir -p $(TABLE1_DIR)
	@for dw in $(TABLE1_SIMPLE_DWS); do \
	    mw=$$(python3 -c "import math; print(math.ceil($(SIMPLE_PARSER_MW)/$$dw)*$$dw)"); \
	    for mode in $(TABLE1_MODES); do \
	        out=$(TABLE1_DIR)/simple_dw$${dw}_$${mode}; \
	        echo "  simple_parser  dw=$$dw  mw=$$mw  mode=$$mode  -->  $$out"; \
	        python3 engine/p4_to_rtl.py \
	            --p4 p4/simple_parser.p4 \
	            --dw $$dw --mw $$mw --cm 1 \
	            --mode $$mode \
	            --o $$out || exit 1; \
	    done; \
	done
	@for dw in $(TABLE1_FULL_DWS); do \
	    mw=$$(python3 -c "import math; print(math.ceil($(FULL_PARSER_MW)/$$dw)*$$dw)"); \
	    for mode in $(TABLE1_MODES); do \
	        out=$(TABLE1_DIR)/full_dw$${dw}_$${mode}; \
	        echo "  full_parser    dw=$$dw  mw=$$mw  mode=$$mode  -->  $$out"; \
	        python3 engine/p4_to_rtl.py \
	            --p4 p4/full_parser.p4 \
	            --dw $$dw --mw $$mw --cm 1 \
	            --mode $$mode \
	            --o $$out || exit 1; \
	    done; \
	done
	@echo "Table 1 RTL written to $(TABLE1_DIR)/"

table1-synth:
	@echo "================================================================"
	@echo " Table 1 — Vivado Synthesis ($(TABLE1_DIR))"
	@echo "================================================================"
	@if ! command -v $(VIVADO) >/dev/null 2>&1; then \
	    echo "Error: Vivado not found. Set VIVADO=/path/to/vivado"; exit 1; \
	fi
	@for d in $(TABLE1_DIR)/*/; do \
	    subdir=$$(cd "$$d" && pwd); \
	    echo "------------------------------------------"; \
	    echo " Synthesizing: $$subdir"; \
	    echo "------------------------------------------"; \
	    $(VIVADO) -mode batch -source $(VIV_TCL_SCRIPT) \
	        -tclargs $(TABLE1_DIR) $$subdir $(TABLE1_XDC) || exit 1; \
	    touch $$subdir/.done; \
	done
	@echo "Synthesis complete."

table1-extract:
	@echo "================================================================"
	@echo " Table 1 — Extracting results → $(TABLE1_RESULTS)"
	@echo "================================================================"
	@python3 table1_extract.py $(TABLE1_DIR) $(TABLE1_RESULTS)


# =============================================================================
# ARTIFACT EVALUATION — Figures 6 & 7 (make fig6 / make fig7)
#
# Fig 6: Simple + Full parser, all DWs, unique config → Quartus → fig6_results.csv
# Fig 7: Simple + Full parser, all DWs, unique config → Vivado  → fig7_results.csv
#
# RTL is shared between both figures (same configs, different tool).
# Individual steps: fig-rtl | fig6-synth | fig6-extract  (same for fig7)
# =============================================================================

fig-rtl:
	@echo "================================================================"
	@echo " Figures 6 & 7 — RTL Generation (unique, dw=$(FIG_DWS))"
	@echo "================================================================"
	@mkdir -p $(FIG_DIR)
	@for dw in $(FIG_DWS); do \
	    mw=$$(python3 -c "import math; print(math.ceil($(SIMPLE_PARSER_MW)/$$dw)*$$dw)"); \
	    out=$(FIG_DIR)/simple_dw$${dw}_unique; \
	    echo "  simple_parser  dw=$$dw  mw=$$mw  -->  $$out"; \
	    python3 engine/p4_to_rtl.py \
	        --p4 p4/simple_parser.p4 \
	        --dw $$dw --mw $$mw --cm 1 \
	        --mode unique \
	        --o $$out || exit 1; \
	done
	@for dw in $(FIG_DWS); do \
	    mw=$$(python3 -c "import math; print(math.ceil($(FULL_PARSER_MW)/$$dw)*$$dw)"); \
	    out=$(FIG_DIR)/full_dw$${dw}_unique; \
	    echo "  full_parser    dw=$$dw  mw=$$mw  -->  $$out"; \
	    python3 engine/p4_to_rtl.py \
	        --p4 p4/full_parser.p4 \
	        --dw $$dw --mw $$mw --cm 1 \
	        --mode unique \
	        --o $$out || exit 1; \
	done
	@echo "Figures RTL written to $(FIG_DIR)/"

fig6: fig-rtl fig6-synth fig6-extract

fig6-synth:
	@echo "================================================================"
	@echo " Figure 6 — Quartus Synthesis ($(FIG_DIR))"
	@echo "================================================================"
	@if ! command -v $(QUARTUS) >/dev/null 2>&1; then \
	    echo "Error: Quartus not found. Set QUARTUS=/path/to/quartus_sh"; exit 1; \
	fi
	@for d in $(FIG_DIR)/*/; do \
	    subdir=$$(cd "$$d" && pwd); \
	    proj_name=$$(basename $$subdir)_proj; \
	    echo "------------------------------------------"; \
	    echo " Synthesizing (Quartus): $$subdir"; \
	    echo "------------------------------------------"; \
	    $(QUARTUS) -t $(QUA_TCL_SCRIPT) $$subdir || exit 1; \
	    $(QUARTUS) --flow compile $$subdir/quartus_proj/$$proj_name || exit 1; \
	    touch $$subdir/.done; \
	done
	@echo "Quartus synthesis complete."

fig6-extract:
	@echo "================================================================"
	@echo " Figure 6 — Extracting results → $(FIG6_CSV)"
	@echo "================================================================"
	@python3 fig_extract.py $(FIG_DIR) $(FIG6_CSV) --tool quartus

fig7: fig-rtl fig7-synth fig7-extract

fig7-synth:
	@echo "================================================================"
	@echo " Figure 7 — Vivado Synthesis ($(FIG_DIR))"
	@echo "================================================================"
	@if ! command -v $(VIVADO) >/dev/null 2>&1; then \
	    echo "Error: Vivado not found. Set VIVADO=/path/to/vivado"; exit 1; \
	fi
	@for d in $(FIG_DIR)/*/; do \
	    subdir=$$(cd "$$d" && pwd); \
	    echo "------------------------------------------"; \
	    echo " Synthesizing (Vivado): $$subdir"; \
	    echo "------------------------------------------"; \
	    $(VIVADO) -mode batch -source $(VIV_TCL_SCRIPT) \
	        -tclargs $(FIG_DIR) $$subdir $(TABLE1_XDC) || exit 1; \
	    touch $$subdir/.done; \
	done
	@echo "Vivado synthesis complete."

fig7-extract:
	@echo "================================================================"
	@echo " Figure 7 — Extracting results → $(FIG7_CSV)"
	@echo "================================================================"
	@python3 fig_extract.py $(FIG_DIR) $(FIG7_CSV) --tool vivado


# =============================================================================
# ARTIFACT EVALUATION — Table 3 (make table3)
#
# Snort 10-rule parser synthesized with both Vivado and Quartus.
# Results (Fmax, LUT, ALM, FF) written side-by-side to table3_results.csv.
#
# Individual steps:
#   table3-rtl            — generate RTL (no tool needed)
#   table3-synth-vivado   — Vivado synthesis
#   table3-synth-quartus  — Quartus synthesis
#   table3-extract        — parse both reports → table3_results.csv
# =============================================================================

table3: table3-rtl table3-synth-vivado table3-synth-quartus table3-extract

table3-rtl:
	@echo "================================================================"
	@echo " Table 3 — Snort RTL Generation ($(SNORT_INPUT))"
	@echo "================================================================"
	@mkdir -p $(TABLE3_DESIGN)
	python3 snort_frontend/create_snort_config.py \
	    --i $(SNORT_INPUT) \
	    --w $(SNORT_WIDTH) \
	    --o snort_frontend/snort_out.hex
	@PYTHONPATH=engine python3 engine/tools/build_rtl_countluts.py \
	    -c snort_frontend/snort_out_config.json \
	    -m snort_frontend/snort_out.hex \
	    -o $(TABLE3_DESIGN) --rtl --tree1 --tree2
	@echo "Snort RTL written to $(TABLE3_DESIGN)/"

table3-synth-vivado:
	@echo "================================================================"
	@echo " Table 3 — Vivado Synthesis"
	@echo "================================================================"
	@if ! command -v $(VIVADO) >/dev/null 2>&1; then \
	    echo "Error: Vivado not found. Set VIVADO=/path/to/vivado"; exit 1; \
	fi
	$(VIVADO) -mode batch -source $(VIV_TCL_SCRIPT) \
	    -tclargs $(dir $(TABLE3_DESIGN)) $(abspath $(TABLE3_DESIGN)) $(TABLE1_XDC) hex_to_rtl_top

table3-synth-quartus:
	@echo "================================================================"
	@echo " Table 3 — Quartus Synthesis"
	@echo "================================================================"
	@if ! command -v $(QUARTUS) >/dev/null 2>&1; then \
	    echo "Error: Quartus not found. Set QUARTUS=/path/to/quartus_sh"; exit 1; \
	fi
	$(QUARTUS) -t $(QUA_TCL_SCRIPT) $(abspath $(TABLE3_DESIGN))
	$(QUARTUS) --flow compile \
	    $(abspath $(TABLE3_DESIGN))/quartus_proj/$(notdir $(TABLE3_DESIGN))_proj

table3-extract:
	@echo "================================================================"
	@echo " Table 3 — Extracting results → $(TABLE3_CSV)"
	@echo "================================================================"
	@python3 table3_extract.py $(TABLE3_DESIGN) $(TABLE3_CSV)


# =============================================================================
# ARTIFACT EVALUATION — Figure 9 (make fig9)
#
# Synthetic benchmarks (unique1+unique2) swept across:
#   - configs: flat (one_group.json) and decomposed (multi_group.json)
#   - widths:  256, 512, 1024 bits
#   - groups:  ug1, ug2, ug3, ug4
# Vivado synthesis only. Results → fig9_results.csv
#
# Individual steps: fig9-rtl | fig9-synth | fig9-extract
# =============================================================================

fig9: fig9-rtl fig9-synth fig9-extract

fig9-rtl:
	@echo "================================================================"
	@echo " Figure 9 — RTL Generation (flat + decomposed, ug1-4)"
	@echo "================================================================"
	@mkdir -p $(FIG9_DIR)
	@for hex in synthetic/synthetic_u*_ug*.hex; do \
	    hex_base=$$(basename $$hex .hex); \
	    width=$$(echo $$hex_base | sed 's/synthetic_u\([0-9]*\)_ug.*/\1/'); \
	    ug=$$(echo $$hex_base | sed 's/.*_ug\([0-9]*\)/\1/'); \
	    for pair in "flat:synthetic/one_group.json" "decomposed:synthetic/multi_group.json"; do \
	        label=$$(echo $$pair | cut -d: -f1); \
	        json=$$(echo $$pair  | cut -d: -f2); \
	        out=$(FIG9_DIR)/$${label}_u$${width}_ug$${ug}; \
	        echo "  $$label  u$$width  ug$$ug  -->  $$out"; \
	        PYTHONPATH=engine python3 engine/tools/build_rtl_countluts.py \
	            -c $$json -m $$hex \
	            -o $$out --rtl --unique1 --unique2 || exit 1; \
	    done; \
	done
	@echo "Figure 9 RTL written to $(FIG9_DIR)/"

fig9-synth:
	@echo "================================================================"
	@echo " Figure 9 — Vivado Synthesis ($(FIG9_DIR))"
	@echo "================================================================"
	@if ! command -v $(VIVADO) >/dev/null 2>&1; then \
	    echo "Error: Vivado not found. Set VIVADO=/path/to/vivado"; exit 1; \
	fi
	@for d in $(FIG9_DIR)/*/; do \
	    subdir=$$(cd "$$d" && pwd); \
	    echo "------------------------------------------"; \
	    echo " Synthesizing: $$subdir"; \
	    echo "------------------------------------------"; \
	    $(VIVADO) -mode batch -source $(VIV_TCL_SCRIPT) \
	        -tclargs $(FIG9_DIR) $$subdir $(TABLE1_XDC) hex_to_rtl_top || exit 1; \
	    touch $$subdir/.done; \
	done
	@echo "Vivado synthesis complete."

fig9-extract:
	@echo "================================================================"
	@echo " Figure 9 — Extracting results → $(FIG9_CSV)"
	@echo "================================================================"
	@python3 fig9_extract.py $(FIG9_DIR) $(FIG9_CSV)


# =============================================================================
# CLEAN
# =============================================================================

clean:
	@echo "Removing generated RTL outputs and temporaries..."
	rm -rf p4_out/ synthetic_out/ snort_out/ artifact_out/
	rm -rf engine/tmp/
	rm -f  snort_frontend/snort_out.hex \
	       snort_frontend/snort_out_config.json \
	       snort_frontend/snort_out_msg_map.txt \
	       $(TABLE1_RESULTS) $(FIG6_CSV) $(FIG7_CSV) $(TABLE3_CSV) $(FIG9_CSV)

clean-synth:
	@echo "Cleaning synthesis projects ($(TOOL)) in $(DIR)..."
	@if [ "$(TOOL)" = "vivado" ]; then \
	    find $(DIR) -type d -name "vivado_proj" -exec rm -rf {} + 2>/dev/null || true; \
	elif [ "$(TOOL)" = "quartus" ]; then \
	    find $(DIR) -type d -name "quartus_proj" -exec rm -rf {} + 2>/dev/null || true; \
	fi
	find $(DIR) -name ".done" -delete 2>/dev/null || true

clean-done:
	find $(DIR) -name ".done" -delete
