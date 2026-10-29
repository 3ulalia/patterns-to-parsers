#include "kernel.h"
#include "ptp_design.h"
#include <ap_axi_sdata.h>
#include <hls_stream.h>

// run on a per-flit basis.
void parser_top(hls::stream<ap_axiu<DATA_WIDTH, 0, 0, 0>> &in,
                hls::stream<ap_uint<ID_WIDTH>> &out) {
#pragma HLS INTERFACE mode = axis port = in
#pragma HLS INTERFACE mode = axis port = out
#pragma HLS INTERFACE mode = ap_ctrl_none port = return
#pragma HLS PIPELINE II = 1

  ap_axiu<DATA_WIDTH, 0, 0, 0> flit;

  // static, since we track these across flits
  static int flit_index = 0;
  static ap_uint<PID_W> pseudo_id = 0;

  flit = in.read();
  bool last = flit.last;

  ap_uint<CMP_W> comparator_bits = compare(flit.data);
  ap_uint<SEG_W> segment_bits = segment_hits(comparator_bits);
  ap_uint<PID_W> captured = capture(pseudo_id, segment_bits, flit_index);
  if (last) {
    ap_uint<ROW_W> matched_rows = row_matches(captured);
    ap_uint<OUT_W> matched_group = winning_group(matched_rows);
    ap_uint<ID_WIDTH> group_id = group_to_id(matched_group);

    out.write(group_id);
    pseudo_id = 0;
  } else {
    pseudo_id = captured;
  }
  flit_index = last ? 0 : (flit_index < N_FLITS ? flit_index + 1 : flit_index);
}
