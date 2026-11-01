mock_provider "oci" {
  mock_data "oci_core_image" {
    defaults = {
      operating_system         = "Canonical Ubuntu"
      operating_system_version = "24.04"
    }
  }
}
variables {
  compartment_ocid    = "ocid1.compartment.oc1..synthetic"
  region              = "eu-frankfurt-1"
  home_region         = "eu-frankfurt-1"
  availability_domain = "synthetic:EU-FRANKFURT-1-AD-1"
  ubuntu_image_ocid   = "ocid1.image.oc1.eu-frankfurt-1.synthetic"
  ssh_public_key      = "ssh-ed25519 SYNTHETIC-PUBLIC-KEY-NOT-FOR-LOGIN"
  ssh_source_cidr     = "192.0.2.1/32"
}
run "refuse_unverified_account" {
  command         = plan
  expect_failures = [oci_core_vcn.pilot]
}
run "refuse_other_region" {
  command = plan
  variables {
    free_tier_verified = true
    region             = "us-ashburn-1"
  }
  expect_failures = [oci_core_vcn.pilot]
}
run "allow_small_verified_free_shape" {
  command = plan
  variables { free_tier_verified = true }
  assert {
    condition     = oci_core_instance.pilot.shape == "VM.Standard.A1.Flex" && oci_core_instance.pilot.shape_config[0].ocpus == 1 && oci_core_instance.pilot.shape_config[0].memory_in_gbs == 6
    error_message = "Do not change the pilot to a paid shape."
  }
  assert {
    condition     = oci_core_volume.data.size_in_gbs + oci_core_instance.pilot.source_details[0].boot_volume_size_in_gbs == 100
    error_message = "Keep combined boot and data capacity at 100 GB."
  }
}
