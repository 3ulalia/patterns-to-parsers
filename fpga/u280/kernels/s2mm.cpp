// AI-generated (Claude, Anthropic); review before relying on it.
// AXIS -> memory: writes the first n_packets IDs from parser_top to device
// memory, one 64-bit word per packet.
#include "ptp_design.h"
#include <ap_int.h>
#include <hls_stream.h>

static_assert(ID_WIDTH <= 64, "IDs wider than 64 bits need a wider ids word");

extern "C" void s2mm(ap_uint<64> *ids, int n_packets,
                     hls::stream<ap_uint<ID_WIDTH>> &ids_in) {
#pragma HLS INTERFACE mode = m_axi port = ids bundle = gmem0
#pragma HLS INTERFACE mode = axis port = ids_in

  for (int packet_index = 0; packet_index < n_packets; packet_index++) {
#pragma HLS PIPELINE II = 1
    ids[packet_index] = ids_in.read();
  }
}
