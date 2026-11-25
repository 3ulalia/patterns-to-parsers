# AI-generated (Claude, Anthropic); review before relying on it.
# U280 build of examples/02_pir_to_rtl, structured after OCT-FPGA/udp-network-demo (2023.1).
#   make -j3 all [TARGET=hw|hw_emu] [FREQ_MHZ=125] [PACKETS=2000]   (-j3 compiles the 3 kernels at once)
#   make all STRATEGIES="Performance_Explore Performance_ExtraTimingOpt"   (2 implementation strategies in parallel)
#   make run [TARGET=...]      (hw_emu runs the emulator on the build machine)
#   make clean | cleanall

DEVICE   ?= xilinx_u280_gen3x16_xdma_1_202211_1
TARGET   ?= hw
FREQ_MHZ ?= 125
PACKETS  ?= 2000

# Tuned for the build server: 8 vCPUs (EPYC 7C13), 64 GB RAM.
#   SYNTH_JOBS: out-of-context synthesis runs at once (kernels + platform IP, a few GB each).
#   THREADS:    Vivado threads per run (general.maxThreads); 8 = one per vCPU.
#   STRATEGIES: implementation strategies run side by side; v++ keeps a run that meets timing.
#               Each U280 implementation run needs roughly 15-25 GB (estimate), so 64 GB fits 2,
#               and 2 runs share the 8 vCPUs at 4 threads each.
comma := ,
empty :=
space := $(empty) $(empty)
SYNTH_JOBS ?= 8
STRATEGIES ?=
ifeq ($(STRATEGIES),)
IMPL_JOBS  ?= 1
THREADS    ?= 8
IMPL_FLAGS :=
else
IMPL_JOBS  ?= 2
THREADS    ?= 4
IMPL_FLAGS := --vivado.impl.strategies "$(subst $(space),$(comma),$(strip $(STRATEGIES)))"
endif

REPO     := $(abspath ../..)
EXAMPLE  := $(REPO)/examples/02_pir_to_rtl
GEN      := build/gen
BUILD    := build/$(TARGET)
XCLBIN   := $(BUILD)/parser.xclbin
HOST_EXE := build/parser_host
CLOCK_NS := $(shell python3 -c "print(1000 / $(FREQ_MHZ))")
FREQ_HZ  := $(shell python3 -c "print(int($(FREQ_MHZ) * 1000000))")

VPP      := $(XILINX_VITIS)/bin/v++
CLFLAGS  := -t $(TARGET) --platform $(DEVICE) --save-temps -I$(GEN)
# kernel.h needs C++17 (if constexpr); PTP_OR_ENCODE matches ptp.run's default
kflags    = --hls.clock $(FREQ_HZ):$(1) --advanced.prop kernel.$(1).kernel_flags="-std=c++17 -DPTP_OR_ENCODE=0"

CXXFLAGS := -g -std=c++17 -I$(XILINX_XRT)/include
LDFLAGS  := -L$(XILINX_XRT)/lib -lxrt_coreutil -pthread

.PHONY: all gen xclbin exe run emconfig clean cleanall check-vitis check-xrt

all: check-vitis check-xrt $(XCLBIN) $(HOST_EXE)

gen: $(GEN)/parser_top.cpp
xclbin: $(XCLBIN)
exe: $(HOST_EXE)

# step 1: kernel sources (ptp_design.h, kernel.h, parser_top.cpp) and test vectors
$(GEN)/parser_top.cpp: $(EXAMPLE)/headers.pir $(EXAMPLE)/headers.json $(wildcard $(REPO)/ptp/*.py $(REPO)/ptp/hls/*)
	PYTHONPATH=$(REPO) python3 -m ptp.run $(EXAMPLE)/headers.pir $(EXAMPLE)/headers.json \
	    -o $(GEN) --backend hls --packets $(PACKETS) --clock $(CLOCK_NS)

# step 2: one .xo per kernel; separate temp/log/report dirs so make -j3 runs don't collide
dirs = --temp_dir $(BUILD)/_x/$(1) --log_dir $(BUILD)/logs/$(1) --report_dir $(BUILD)/reports/$(1)
$(BUILD)/parser_top.xo: $(GEN)/parser_top.cpp
	mkdir -p $(BUILD)
	$(VPP) $(CLFLAGS) $(call kflags,parser_top) -c -k parser_top $(call dirs,parser_top) -o $@ $<
$(BUILD)/%.xo: kernels/%.cpp $(GEN)/parser_top.cpp
	mkdir -p $(BUILD)
	$(VPP) $(CLFLAGS) $(call kflags,$*) -c -k $* $(call dirs,$*) -o $@ $<

# step 3: link
$(XCLBIN): $(BUILD)/mm2s.xo $(BUILD)/parser_top.xo $(BUILD)/s2mm.xo connectivity.ini
	$(VPP) $(CLFLAGS) -l --config connectivity.ini --kernel_frequency $(FREQ_MHZ) \
	    --vivado.synth.jobs $(SYNTH_JOBS) --vivado.impl.jobs $(IMPL_JOBS) \
	    --vivado.param general.maxThreads=$(THREADS) $(IMPL_FLAGS) \
	    $(call dirs,link) -o $@ $(filter %.xo,$^)

# step 4: host program
$(HOST_EXE): host/host.cpp
	mkdir -p build
	g++ $(CXXFLAGS) $< -o $@ $(LDFLAGS)

emconfig: build/emconfig.json
build/emconfig.json:
	mkdir -p build
	emconfigutil --platform $(DEVICE) --od build

# step 5: run (the host loads the xclbin itself; no xbutil program needed)
ifeq ($(TARGET),hw)
run: all
	./$(HOST_EXE) $(XCLBIN) $(GEN)
else
run: all emconfig
	XCL_EMULATION_MODE=$(TARGET) EMCONFIG_PATH=build ./$(HOST_EXE) $(XCLBIN) $(GEN) 0 600000
endif

check-vitis:
ifndef XILINX_VITIS
	$(error XILINX_VITIS is not set; source <Vitis>/settings64.sh)
endif

check-xrt:
ifndef XILINX_XRT
	$(error XILINX_XRT is not set; source /opt/xilinx/xrt/setup.sh)
endif

clean:
	rm -rf *v++* *.log *.jou *.str .run .ipcache .Xil $(HOST_EXE)

cleanall: clean
	rm -rf build _x* *.xclbin *.info *.link_summary *.ltx
