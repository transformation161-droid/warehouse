terraform {
  required_version = ">= 1.7.0, < 2.0.0"
  required_providers {
    oci = {
      source  = "oracle/oci"
      version = "9.11.0"
    }
  }
}

# Authentication uses the operator's secured OCI CLI configuration or instance identity.
# Never put private keys, passwords or tokens in Terraform variables or user_data.
provider "oci" {
  region = var.region
}

variable "compartment_ocid" { type = string }
variable "region" { type = string }
variable "home_region" { type = string }
variable "availability_domain" { type = string }
variable "ubuntu_image_ocid" { type = string }
variable "ssh_public_key" {
  type = string
  validation {
    condition     = can(regex("^ssh-(ed25519|rsa) ", var.ssh_public_key))
    error_message = "Provide an SSH PUBLIC key only."
  }
}
variable "ssh_source_cidr" {
  type = string
  validation {
    condition     = can(cidrhost(var.ssh_source_cidr, 0)) && can(regex("/32$", var.ssh_source_cidr))
    error_message = "SSH must be restricted to the operator's single IPv4 /32."
  }
}
variable "free_tier_verified" {
  type        = bool
  default     = false
  description = "Set only after verifying Free Tier account, Always Free eligibility and unused account-wide allowances; otherwise planning fails."
}

resource "oci_core_vcn" "pilot" {
  compartment_id = var.compartment_ocid
  cidr_blocks    = ["10.42.0.0/24"]
  display_name   = "ksu-free-pilot"
  lifecycle {
    precondition {
      condition     = var.free_tier_verified && var.region == var.home_region
      error_message = "STOP: verify Always Free account and remaining limits in the home region. No paid fallback is allowed."
    }
  }
}
resource "oci_core_internet_gateway" "pilot" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.pilot.id
  display_name   = "ksu-free-pilot"
}
resource "oci_core_route_table" "pilot" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.pilot.id
  route_rules {
    destination       = "0.0.0.0/0"
    destination_type  = "CIDR_BLOCK"
    network_entity_id = oci_core_internet_gateway.pilot.id
  }
}
resource "oci_core_security_list" "pilot" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.pilot.id
  egress_security_rules {
    destination = "0.0.0.0/0"
    protocol    = "all"
  }
  ingress_security_rules {
    source   = var.ssh_source_cidr
    protocol = "6"
    tcp_options {
      min = 22
      max = 22
    }
  }
  dynamic "ingress_security_rules" {
    for_each = [80, 443]
    content {
      source   = "0.0.0.0/0"
      protocol = "6"
      tcp_options {
        min = ingress_security_rules.value
        max = ingress_security_rules.value
      }
    }
  }
  ingress_security_rules {
    source   = "0.0.0.0/0"
    protocol = "1"
    icmp_options {
      type = 3
      code = 4
    }
  }
}
resource "oci_core_subnet" "pilot" {
  compartment_id    = var.compartment_ocid
  vcn_id            = oci_core_vcn.pilot.id
  cidr_block        = "10.42.0.0/24"
  route_table_id    = oci_core_route_table.pilot.id
  security_list_ids = [oci_core_security_list.pilot.id]
}
data "oci_core_image" "ubuntu" {
  image_id = var.ubuntu_image_ocid
}
resource "oci_core_instance" "pilot" {
  availability_domain = var.availability_domain
  compartment_id      = var.compartment_ocid
  display_name        = "ksu-free-pilot"
  shape               = "VM.Standard.A1.Flex"
  shape_config {
    ocpus         = 1
    memory_in_gbs = 6
  }
  create_vnic_details {
    subnet_id        = oci_core_subnet.pilot.id
    assign_public_ip = true
  }
  source_details {
    source_type             = "image"
    source_id               = var.ubuntu_image_ocid
    boot_volume_size_in_gbs = 50
    boot_volume_vpus_per_gb = 10
  }
  metadata = {
    ssh_authorized_keys = trimspace(var.ssh_public_key)
  }
  freeform_tags = { purpose = "synthetic-ksu-free-pilot" }
  lifecycle {
    precondition {
      condition     = data.oci_core_image.ubuntu.operating_system == "Canonical Ubuntu" && data.oci_core_image.ubuntu.operating_system_version == "24.04"
      error_message = "Use the official Ubuntu 24.04 ARM image, not a paid Marketplace image."
    }
  }
}
resource "oci_core_volume" "data" {
  availability_domain = var.availability_domain
  compartment_id      = var.compartment_ocid
  display_name        = "ksu-free-persistent-data"
  size_in_gbs         = 50
  vpus_per_gb         = 10
  lifecycle { prevent_destroy = true }
}
resource "oci_core_volume_attachment" "data" {
  attachment_type = "paravirtualized"
  instance_id     = oci_core_instance.pilot.id
  volume_id       = oci_core_volume.data.id
  device          = "/dev/oracleoci/oraclevdb"
}
output "public_ip" { value = oci_core_instance.pilot.public_ip }
output "suggested_hostname" { value = "ksu.${replace(oci_core_instance.pilot.public_ip, ".", "-")}.sslip.io" }
output "persistent_volume_id" { value = oci_core_volume.data.id }
