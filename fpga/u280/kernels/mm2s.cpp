// AI-generated (Claude, Anthropic); review before relying on it.
// Memory -> AXIS: streams n_flits flits from device memory into parser_top.
// ctrl holds one vec_ctrl.hex byte per flit; bit 0 is TLAST.
#include "ptp_design.h"
#include <ap_axi_sdata.h>
#include <hls_stream.h>

static_assert(DATA_WIDTH >= 8 && DATA_WIDTH <= 1024 &&
                  (DATA_WIDTH & (DATA_WIDTH - 1)) == 0,
              "m_axi data width must be a power of two from 8 to 1024 bits");

extern "C" void mm2s(const ap_uint<DATA_WIDTH> *flits, const ap_uint<8> *ctrl,
                     int n_flits,
                     hls::stream<ap_axiu<DATA_WIDTH, 0, 0, 0>> &flits_out) {
#pragma HLS INTERFACE mode = m_axi port = flits bundle = gmem0
#pragma HLS INTERFACE mode = m_axi port = ctrl bundle = gmem1
#pragma HLS INTERFACE mode = axis port = flits_out

  for (int flit_index = 0; flit_index < n_flits; flit_index++) {
#pragma HLS PIPELINE II = 1
    ap_axiu<DATA_WIDTH, 0, 0, 0> beat;
    beat.data = flits[flit_index];
    beat.last = ctrl[flit_index][0];
    beat.keep = -1;
    beat.strb = -1;
    flits_out.write(beat);
  }
}
