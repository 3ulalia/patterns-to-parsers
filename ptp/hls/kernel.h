#pragma once
#include "ptp_design.h"

#ifndef PTP_OR_ENCODE
#define PTP_OR_ENCODE 0
#endif
inline int extract_nibble(const ap_uint<DATA_WIDTH> &flit,
                          int nibble_position) {
  return (flit >> (DATA_WIDTH - 4 - 4 * nibble_position)) & 0xF;
};
inline ap_uint<CMP_W> compare(const ap_uint<DATA_WIDTH> &flit) {
  ap_uint<CMP_W> out = 0;

  for (int pos = 0; pos < N_NIBBLE_COMPARATORS; pos++) {
#pragma HLS unroll
    out[pos] = CMP_V[pos] == extract_nibble(flit, CMP_J[pos]);
  }
  return out;
};

inline ap_uint<SEG_W> segment_hits(const ap_uint<CMP_W> &comparator_bits) {
  ap_uint<SEG_W> out = 0;
  for (int segment = 0; segment < N_SEGMENTS; segment++) {
#pragma HLS unroll
    bool hit = true;
    int lower_offset = SEGMENT_TERM_OFFSETS[segment];
    int upper_offset = SEGMENT_TERM_OFFSETS[segment + 1];
    for (int term = lower_offset; term < upper_offset; term++) {
#pragma HLS unroll
      hit &= comparator_bits[ALL_SEGMENT_TERMS[term]];
    }
    out[segment] = hit;
  }
  return out;
};

inline ap_uint<PID_W> encode_column(int column, const ap_uint<SEG_W> &hits) {
  ap_uint<PID_W> out = 0;
  int first_segment = COLUMN_SEGMENT_OFFSETS[column];
  int last_segment = COLUMN_SEGMENT_OFFSETS[column + 1];

  if (COLUMN_IS_ONEHOT[column]) {
    for (int segment = first_segment; segment < last_segment; segment++) {
#pragma HLS unroll
      if (hits[segment]) {
        out[SEGMENT_IDS[segment] - 1] = 1;
      }
    }
    return out;
  } else {

    for (int segment = first_segment; segment < last_segment; segment++) {
#pragma HLS unroll
      if (hits[segment]) {
        if constexpr (PTP_OR_ENCODE) {
          out |= SEGMENT_IDS[segment];
        } else {
          out = SEGMENT_IDS[segment];
        }
      }
    }
    return out;
  }
};

inline ap_uint<PID_W> capture(const ap_uint<PID_W> &pid,
                              const ap_uint<SEG_W> &hits, int flit_index) {
  ap_uint<PID_W> out = pid;
  for (int column = 0; column < N_COLUMNS; column++) {
#pragma HLS unroll
    if (COLUMN_FLIT_INDICES[column] == flit_index) {
      int offset = COLUMN_PSID_OFFSETS[column];
      out.range( // I love modern C++!
          offset + COLUMN_PSID_WIDTHS[column] - 1, offset) =
          encode_column(column, hits);
    }
  }

  return out;
};

inline ap_uint<ROW_W> row_matches(const ap_uint<PID_W> &pid) {

  ap_uint<ROW_W> out = 0;
  for (int row = 0; row < N_ROWS; row++) {
#pragma HLS unroll
    bool all_terms_hold = true;
    for (int term = ROW_TERM_OFFSETS[row]; term < ROW_TERM_OFFSETS[row + 1];
         term++) {
#pragma HLS unroll
      int column = ROW_TERM_COLUMNS[term];
      int segment_id = ROW_TERM_SEGMENT_IDS[term];

      int low_pid = COLUMN_PSID_OFFSETS[column];

      ap_uint<PID_W> pseudo_id =
          pid.range(low_pid + COLUMN_PSID_WIDTHS[column] - 1, low_pid);
      bool term_holds = COLUMN_IS_ONEHOT[column] ? pseudo_id[segment_id - 1]
                                                 : pseudo_id == segment_id;

      all_terms_hold &= term_holds;
    }
    out[row] = all_terms_hold;
  }
  return out;
};

inline ap_uint<OUT_W> winning_group(const ap_uint<ROW_W> &match_bits) {

  ap_uint<OUT_W> out = 0;

  for (int row = 0; row < N_ROWS; row++) {
#pragma HLS unroll
    int group = ROW_GROUPS[row];

    if (group < 0 || (!match_bits[row])) // the row is a mask (negative)
      continue;

    bool cont = false;
    for (int masking_row = ROW_MASK_OFFSETS[row];
         masking_row < ROW_MASK_OFFSETS[row + 1]; masking_row++) {
#pragma HLS unroll
      if (match_bits[ROW_MASK_INDICES[masking_row]]) {
        cont = true;
      }
      if (cont)
        break;
    }
    if (cont)
      continue;
    out[group] = 1;
  }
  return out;
};

inline ap_uint<ID_WIDTH> group_to_id(const ap_uint<OUT_W> &group) {

  ap_uint<ID_WIDTH> out = 0;
  for (int group_number = 0; group_number < N_OUTPUTS; group_number++) {
#pragma HLS unroll
    if (group[group_number]) {
      if constexpr (PTP_OR_ENCODE) {
        out |= GROUP_IDS[group_number];
      } else {
        out = GROUP_IDS[group_number];
      }
    }
  }
  return out;
};
