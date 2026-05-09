// SPDX-License-Identifier: MIT
/* -*- P4_16 -*- */
/* Full parser: Ethernet + up to 2 VLANs + up to 2 MPLS labels
   + IPv4/IPv6 (TCP/UDP/ICMP) + up to 2 IPv6 extension headers */
#include <core.p4>
#include <v1model.p4>

const bit<16> TYPE_IPV4     = 0x0800;
const bit<16> TYPE_IPV6     = 0x86dd;
const bit<16> TYPE_VLAN     = 0x8100;
const bit<16> TYPE_MPLS     = 0x8847;
const bit<8>  TYPE_TCP      = 0x06;
const bit<8>  TYPE_UDP      = 0x11;
const bit<8>  TYPE_ICMP     = 0x01;
const bit<8>  TYPE_IPV6_EXT = 0x2B;

error { IPv4PacketError, IPv6PacketError }

/*************************************************************************
 * H E A D E R S
 *************************************************************************/

typedef bit<32> ip4Addr_t;

header ethernet_t {
    bit<48> dstAddr;
    bit<48> srcAddr;
    bit<16> etherType;
}

header vlan_t {
    bit<3>  pcp;
    bit<1>  dei;
    bit<12> vid;
    bit<16> etherType;
}

header mpls_t {
    bit<20> label;
    bit<3>  tc;
    bit<1>  bos;
    bit<8>  ttl;
}

/* First 48 bits after the MPLS stack — overlaps the IPv4/IPv6 header start.
   Used only for IP version detection; remaining IP bits extracted separately. */
header mpls_fin_t {
    bit<4>  version;
    bit<8>  tc_or_ihl_dscp;
    bit<20> flow_or_rest;
    bit<16> plen_or_totlen;
}

/* Remaining 112 bits of IPv4 header after mpls_fin consumed the first 48. */
header ipv4_mpls_rest_t {
    bit<3>    flags;
    bit<13>   fragOffset;
    bit<8>    ttl;
    bit<8>    protocol__prot__;
    bit<16>   hdrChecksum;
    ip4Addr_t srcAddr__sip__;
    ip4Addr_t dstAddr__dip__;
}

/* Remaining 272 bits of IPv6 header after mpls_fin consumed the first 48. */
header ipv6_mpls_rest_t {
    bit<8>   nextHdr__prot__;
    bit<8>   hopLimit;
    bit<128> srcAddr__sip__;
    bit<128> dstAddr__dip__;
}

header ipv4_t {
    bit<4>    version;
    bit<4>    ihl;
    bit<8>    diffserv;
    bit<16>   totalLen;
    bit<16>   identification;
    bit<3>    flags;
    bit<13>   fragOffset;
    bit<8>    ttl;
    bit<8>    protocol__prot__;
    bit<16>   hdrChecksum;
    ip4Addr_t srcAddr__sip__;
    ip4Addr_t dstAddr__dip__;
}

header ipv6_t {
    bit<4>   version;
    bit<8>   trafficClass;
    bit<20>  flowLabel;
    bit<16>  payloadLen;
    bit<8>   nextHdr__prot__;
    bit<8>   hopLimit;
    bit<128> srcAddr__sip__;
    bit<128> dstAddr__dip__;
}

/* IPv6 extension header (routing / hop-by-hop): 8 bytes = 64 bits */
header ipv6_ext_t {
    bit<8>  nextHdr__prot__;
    bit<8>  extHdrLen;
    bit<48> data;
}

header tcp_t {
    bit<16> srcPort__sp__;
    bit<16> dstPort__dp__;
    bit<32> seqNo;
    bit<32> ackNo;
    bit<4>  dataOffset;
    bit<4>  res;
    bit<8>  flags;
    bit<16> window;
    bit<16> checksum;
    bit<16> urgentPtr;
}

header udp_t {
    bit<16> srcPort__sp__;
    bit<16> dstPort__dp__;
    bit<16> length_;
    bit<16> checksum;
}

/* ICMP: consume 64 bits (matches icmp_fin state in the JSON graph) */
header icmp_t {
    bit<64> data;
}

struct metadata { /* empty */ }

struct headers {
    ethernet_t       ethernet;
    vlan_t           outer_vlan;
    vlan_t           outer_vlan2;
    mpls_t           mpls1;
    mpls_t           mpls2;
    mpls_fin_t       mpls_fin;
    ipv4_mpls_rest_t ipv4_mpls_rest;
    ipv6_mpls_rest_t ipv6_mpls_rest;
    ipv4_t           ipv4;
    ipv6_t           ipv6;
    ipv6_ext_t       ipv6_ext1;
    ipv6_ext_t       ipv6_ext2;
    tcp_t            tcp;
    udp_t            udp;
    icmp_t           icmp;
}

/*************************************************************************
 * P A R S E R
 *************************************************************************/

parser MyParser(packet_in packet,
                out headers hdr,
                inout metadata meta,
                inout standard_metadata_t standard_metadata) {

    state start {
        transition parse_ethernet;
    }

    // ------------------------------------------------------------------
    // L2: Ethernet → VLAN / MPLS / IP
    // ------------------------------------------------------------------

    state parse_ethernet {
        packet.extract(hdr.ethernet);
        transition select(hdr.ethernet.etherType) {
            TYPE_IPV4: parse_ipv4;
            TYPE_IPV6: parse_ipv6;
            TYPE_VLAN: parse_outer_vlan;
            TYPE_MPLS: parse_mpls1;
            default:   accept;
        }
    }

    state parse_outer_vlan {
        packet.extract(hdr.outer_vlan);
        transition select(hdr.outer_vlan.etherType) {
            TYPE_IPV4: parse_ipv4;
            TYPE_IPV6: parse_ipv6;
            TYPE_VLAN: parse_outer_vlan2;
            TYPE_MPLS: parse_mpls1;
            default:   accept;
        }
    }

    state parse_outer_vlan2 {
        packet.extract(hdr.outer_vlan2);
        transition select(hdr.outer_vlan2.etherType) {
            TYPE_IPV4: parse_ipv4;
            TYPE_IPV6: parse_ipv6;
            TYPE_MPLS: parse_mpls1;
            default:   accept;
        }
    }

    // ------------------------------------------------------------------
    // MPLS stack (up to 2 labels) → IP version detection
    // ------------------------------------------------------------------

    state parse_mpls1 {
        packet.extract(hdr.mpls1);
        transition select(hdr.mpls1.bos) {
            1: parse_mpls_fin;
            0: parse_mpls2;
        }
    }

    state parse_mpls2 {
        packet.extract(hdr.mpls2);
        transition parse_mpls_fin;
    }

    state parse_mpls_fin {
        packet.extract(hdr.mpls_fin);
        transition select(hdr.mpls_fin.version) {
            4: parse_ipv4_after_mpls;
            6: parse_ipv6_after_mpls;
            default: accept;
        }
    }

    // Post-MPLS IPv4: extract remaining 112 bits of the IPv4 header
    state parse_ipv4_after_mpls {
        packet.extract(hdr.ipv4_mpls_rest);
        transition select(hdr.ipv4_mpls_rest.protocol__prot__) {
            TYPE_TCP:  parse_tcp;
            TYPE_UDP:  parse_udp;
            TYPE_ICMP: parse_icmp;
            default:   accept;
        }
    }

    // Post-MPLS IPv6: extract remaining 272 bits of the IPv6 header,
    // then share the same ext/L4 states as the direct IPv6 path.
    state parse_ipv6_after_mpls {
        packet.extract(hdr.ipv6_mpls_rest);
        transition select(hdr.ipv6_mpls_rest.nextHdr__prot__) {
            TYPE_TCP:      parse_tcp;
            TYPE_UDP:      parse_udp;
            TYPE_ICMP:     parse_icmp;
            TYPE_IPV6_EXT: parse_ipv6_ext1;
            default:       accept;
        }
    }

    // ------------------------------------------------------------------
    // Direct IP paths (no MPLS)
    // ------------------------------------------------------------------

    state parse_ipv4 {
        packet.extract(hdr.ipv4);
        transition select(hdr.ipv4.version, hdr.ipv4.ihl, hdr.ipv4.protocol__prot__) {
            (4, 5, TYPE_TCP):  parse_tcp;
            (4, 5, TYPE_UDP):  parse_udp;
            (4, 5, TYPE_ICMP): parse_icmp;
            default:           accept;
        }
    }

    state parse_ipv6 {
        packet.extract(hdr.ipv6);
        verify(hdr.ipv6.version == 6, error.IPv6PacketError);
        transition select(hdr.ipv6.version, hdr.ipv6.nextHdr__prot__) {
            (6, TYPE_TCP):      parse_tcp;
            (6, TYPE_UDP):      parse_udp;
            (6, TYPE_ICMP):     parse_icmp;
            (6, TYPE_IPV6_EXT): parse_ipv6_ext1;
            default:            accept;
        }
    }

    // ------------------------------------------------------------------
    // IPv6 extension headers (shared by direct and post-MPLS paths)
    // ------------------------------------------------------------------

    state parse_ipv6_ext1 {
        packet.extract(hdr.ipv6_ext1);
        transition select(hdr.ipv6_ext1.nextHdr__prot__) {
            TYPE_TCP:      parse_tcp;
            TYPE_UDP:      parse_udp;
            TYPE_ICMP:     parse_icmp;
            TYPE_IPV6_EXT: parse_ipv6_ext2;
            default:       accept;
        }
    }

    state parse_ipv6_ext2 {
        packet.extract(hdr.ipv6_ext2);
        transition select(hdr.ipv6_ext2.nextHdr__prot__) {
            TYPE_TCP:  parse_tcp;
            TYPE_UDP:  parse_udp;
            TYPE_ICMP: parse_icmp;
            default:   accept;
        }
    }

    // ------------------------------------------------------------------
    // L4
    // ------------------------------------------------------------------

    state parse_tcp {
        packet.extract(hdr.tcp);
        transition accept;
    }

    state parse_udp {
        packet.extract(hdr.udp);
        transition accept;
    }

    state parse_icmp {
        packet.extract(hdr.icmp);
        transition accept;
    }
}

/*************************************************************************
 * D U M M Y   B L O C K S   ( R E Q U I R E D   B Y   V 1 M O D E L )
 *************************************************************************/

control MyVerifyChecksum(inout headers hdr, inout metadata meta) { apply { } }
control MyIngress(inout headers hdr, inout metadata meta, inout standard_metadata_t standard_metadata) { apply { } }
control MyEgress(inout headers hdr, inout metadata meta, inout standard_metadata_t standard_metadata) { apply { } }
control MyComputeChecksum(inout headers hdr, inout metadata meta) { apply { } }
control MyDeparser(packet_out packet, in headers hdr) { apply { } }

V1Switch(
    MyParser(),
    MyVerifyChecksum(),
    MyIngress(),
    MyEgress(),
    MyComputeChecksum(),
    MyDeparser()
) main;
