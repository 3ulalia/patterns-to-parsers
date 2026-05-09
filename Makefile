# =============================================================================
# PatternToParser — Push-button RTL generation and synthesis flows
#
# RTL generation (Python + jinja2; p4c only needed for the P4 flow):
#   make p4          — compile P4 parsers to SystemVerilog  →  p4_out/
#   make synthetic   — generate RTL from synthetic benchmarks →  synthetic_out/
#   make snort       — generate RTL from Snort rules          →  snort_out/
#   make all         — run all three flows
#
# Synthesis (requires Vivado or Quartus):
#   make synth   TOOL=vivado   DIR=p4_out/full_parser
#   make synth   TOOL=quartus  DIR=p4_out/full_parser
#   make extract TOOL=vivado   DIR=p4_out/full_parser
#
# Override tool paths if not on PATH:
#   make synth VIVADO=/path/to/vivado QUARTUS=/path/to/quartus_sh
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

.PHONY: all p4 p4_full p4_simple synthetic snort \
        synth run extract clean clean-synth clean-done


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
# CLEAN
# =============================================================================

clean:
	@echo "Removing generated RTL outputs and temporaries..."
	rm -rf p4_out/ synthetic_out/ snort_out/
	rm -rf engine/tmp/
	rm -f  snort_frontend/snort_out.hex \
	       snort_frontend/snort_out_config.json \
	       snort_frontend/snort_out_msg_map.txt

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
