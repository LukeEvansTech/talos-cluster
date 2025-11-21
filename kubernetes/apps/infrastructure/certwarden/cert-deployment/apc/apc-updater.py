#!/usr/bin/env python3

# vim: autoindent tabstop=4 shiftwidth=4 expandtab softtabstop=4 filetype=python

# This file is part of APC NMC certificate updater.
# APC NMC certificate updater is free software: you can
# redistribute it and/or modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation, version 2.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program; if not, write to the Free Software Foundation, Inc., 51
# Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.

import os
import sys
import argparse
import subprocess
import tempfile
import logging

REQUEST_TIMEOUT = 30.0

class APCUpdater:
    def __init__(self, hostname, username, password, fingerprint, apc_tool_path,
                 insecure_cipher=False, debug=False):
        self.hostname = hostname
        self.username = username
        self.password = password
        self.fingerprint = fingerprint
        self.apc_tool_path = apc_tool_path
        self.insecure_cipher = insecure_cipher
        self.debug = debug

        # Setup logging
        log_level = logging.DEBUG if debug else logging.INFO
        logging.basicConfig(
            level=log_level,
            format='%(asctime)s - %(levelname)s - %(message)s'
        )
        self.logger = logging.getLogger("APCUpdater")

    def install_cert(self, key_file, cert_file):
        """
        Install certificate to APC NMC using apc-p15-tool
        :param key_file: filename to X.509 certificate private key
        :param cert_file: filename to X.509 certificate PEM
        :return: bool
        """
        self.logger.info(f"Installing certificate to APC NMC at {self.hostname}")
        self.logger.debug(f"Reading certificate from {cert_file}")
        self.logger.debug(f"Reading key from {key_file}")

        # Verify files exist
        if not os.path.isfile(key_file):
            self.logger.error(f"Key file '{key_file}' doesn't exist!")
            return False
        if not os.path.isfile(cert_file):
            self.logger.error(f"Certificate file '{cert_file}' doesn't exist!")
            return False

        # Build the apc-p15-tool command
        cmd = [
            self.apc_tool_path,
            'install',
            '--keyfile', key_file,
            '--certfile', cert_file,
            '--hostname', self.hostname,
            '--username', self.username,
            '--password', self.password,
            '--fingerprint', self.fingerprint
        ]

        # Add insecure cipher flag if needed (required for older APC devices with cryptlib SSH)
        if self.insecure_cipher:
            cmd.append('--insecurecipher')
            self.logger.warning("Using --insecurecipher flag for legacy SSH support")

        if self.debug:
            cmd.append('--debug')

        self.logger.debug(f"Executing command: {' '.join(cmd[:7])} [credentials hidden]")

        try:
            # Run the apc-p15-tool command
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=REQUEST_TIMEOUT
            )

            # Log output
            if result.stdout:
                self.logger.info(f"apc-p15-tool output:\n{result.stdout}")
            if result.stderr:
                self.logger.warning(f"apc-p15-tool stderr:\n{result.stderr}")

            # Check return code
            if result.returncode != 0:
                self.logger.error(f"apc-p15-tool failed with return code {result.returncode}")
                return False

            self.logger.info("✅ Certificate installed successfully!")
            return True

        except subprocess.TimeoutExpired:
            self.logger.error(f"apc-p15-tool timed out after {REQUEST_TIMEOUT} seconds")
            return False
        except Exception as e:
            self.logger.error(f"Failed to execute apc-p15-tool: {e}")
            return False


def main():
    parser = argparse.ArgumentParser(description='Update APC NMC SSL certificate')
    parser.add_argument('--hostname', required=True,
                        help='APC NMC hostname or IP address')
    parser.add_argument('--username', required=True,
                        help='APC NMC username with admin access')
    parser.add_argument('--password', required=True,
                        help='APC NMC user password')
    parser.add_argument('--fingerprint', required=True,
                        help='APC NMC SSH host key fingerprint')
    parser.add_argument('--key-file', required=True,
                        help='X.509 Private key filename')
    parser.add_argument('--cert-file', required=True,
                        help='X.509 Certificate filename')
    parser.add_argument('--apc-tool-path', default='/usr/local/bin/apc-p15-tool',
                        help='Path to apc-p15-tool binary (default: /usr/local/bin/apc-p15-tool)')
    parser.add_argument('--insecure-cipher', action='store_true',
                        help='Use insecure ciphers for older APC devices (--insecurecipher)')
    parser.add_argument('--quiet', action='store_true',
                        help='Do not output anything if successful')
    parser.add_argument('--debug', action='store_true',
                        help='Output additional debugging')
    args = parser.parse_args()

    # Confirm args
    if not os.path.isfile(args.key_file):
        print(f"--key-file '{args.key_file}' doesn't exist!")
        sys.exit(2)
    if not os.path.isfile(args.cert_file):
        print(f"--cert-file '{args.cert_file}' doesn't exist!")
        sys.exit(2)
    if not os.path.isfile(args.apc_tool_path):
        print(f"apc-p15-tool not found at '{args.apc_tool_path}'!")
        print("Make sure the apc-p15-tool binary is installed")
        sys.exit(2)

    # Create updater
    updater = APCUpdater(
        hostname=args.hostname,
        username=args.username,
        password=args.password,
        fingerprint=args.fingerprint,
        apc_tool_path=args.apc_tool_path,
        insecure_cipher=args.insecure_cipher,
        debug=args.debug
    )

    # Install certificate
    if not updater.install_cert(args.key_file, args.cert_file):
        print("Failed to install certificate to APC NMC!")
        sys.exit(2)

    if not args.quiet:
        print("All done!")

    sys.exit(0)


if __name__ == "__main__":
    main()
