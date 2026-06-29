resource "aws_efs_file_system" "odoo" {
  creation_token   = "odoo-efs"
  performance_mode = "generalPurpose"

  # Why Elastic? Small Odoo systems (1-10GB) would be throttled 
  # to ~50-500 KiB/s in Bursting mode (unless we have burst credits accumulated),
  # making the UI unusable.
  # Elastic provides on-demand performance regardless of size.
  # Since we use Archive for old files, the per-GB transfer cost 
  # is only paid on active files, keeping costs predictable.
  # 95% of the files endup in Archive storage making saving from
  # Bursting redundant. NOTE: Archive is not available in Bursting mode
  throughput_mode = "elastic"

  # Move to Infrequent Access after 30 days
  lifecycle_policy {
    transition_to_ia = "AFTER_30_DAYS"
  }

  # Archive storage: Move to Archive after 90 days.
  # Saves ~95% on storage costs for old attachments/logs.
  lifecycle_policy {
    transition_to_archive = "AFTER_90_DAYS"
  }

  # Move back to Standard immediately on access
  lifecycle_policy {
    transition_to_primary_storage_class = "AFTER_1_ACCESS"
  }
}

resource "aws_efs_mount_target" "efs_mount" {
  file_system_id  = aws_efs_file_system.odoo.id
  subnet_id       = aws_subnet.private_a.id
  security_groups = [aws_security_group.efs_sg.id]
}

resource "aws_efs_mount_target" "efs_mount2" {
  file_system_id  = aws_efs_file_system.odoo.id
  subnet_id       = aws_subnet.private_b.id
  security_groups = [aws_security_group.efs_sg.id]
}

resource "aws_efs_access_point" "odoo" {
  file_system_id = aws_efs_file_system.odoo.id


  # Automatically creates a folder with the right permissions if it doesn't exist
  # Odoo is the uid 101
  root_directory {
    path = "/odoo-data"
    creation_info {
      owner_uid   = 101
      owner_gid   = 101
      permissions = "0755"
    }
  }
}
