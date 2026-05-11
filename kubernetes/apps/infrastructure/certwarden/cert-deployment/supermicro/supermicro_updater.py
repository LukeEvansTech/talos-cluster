#!/usr/bin/env python3
"""Supermicro IPMI Certificate Updater (Redfish API).

Supports X12, X13, and H13 motherboards using Redfish API.

This is a simplified version that only supports modern Supermicro boards
with Redfish API. Legacy X9/X10/X11 support has been removed.

Copyright (c) Jari Turkia (original)
Modified for Redfish-only support
"""

import argparse
import http.client as http_client
import json
import logging
import os
import re
import sys
from datetime import datetime

import requests
from OpenSSL import crypto as openssl_crypto

REQUEST_TIMEOUT = 30.0


class RedfishIPMIUpdater:
    """IPMI certificate updater for Redfish-based Supermicro boards (X12/X13/H13)."""

    def __init__(self, session, ipmi_url):
        """Initialise the updater with an HTTP session and IPMI base URL."""
        self.session = session
        self.ipmi_url = ipmi_url.rstrip("/")

        # Redfish API endpoints
        self.login_url = f"{ipmi_url}/redfish/v1/SessionService/Sessions"
        self.cert_info_url = (
            f"{ipmi_url}/redfish/v1/UpdateService/Oem/Supermicro/SSLCert"
        )
        self.upload_cert_url = (
            f"{ipmi_url}/redfish/v1/UpdateService/Oem/Supermicro/SSLCert"
            f"/Actions/SmcSSLCert.Upload"
        )
        self.reboot_url = f"{ipmi_url}/redfish/v1/Managers/1/Actions/Manager.Reset"

        error_log = logging.getLogger("RedfishIPMIUpdater")
        error_log.setLevel(logging.ERROR)
        self.logger = error_log

    def set_logger(self, logger):
        """Attach a logger instance to this updater."""
        self.logger = logger

    def login(self, username, password):
        """Log into IPMI using Redfish API.

        :param username: IPMI username
        :param password: IPMI password
        :return: response object or False
        """
        print(f"DEBUG: Logging in via Redfish to {self.login_url}")

        login_data = {"UserName": username, "Password": password}

        request_headers = {"Content-Type": "application/json"}

        try:
            result = self.session.post(
                self.login_url,
                data=json.dumps(login_data),
                headers=request_headers,
                timeout=REQUEST_TIMEOUT,
                verify=False,
            )
        except requests.RequestException as e:
            print(f"ERROR: Connection error during login: {e}")
            return False

        if not result.ok:
            print(f"ERROR: Login failed with status code: {result.status_code}")
            print(f"ERROR: Response: {result.text}")
            return False

        print("DEBUG: Login successful, got auth token")
        return result

    def get_ipmi_cert_info(self, token):
        """Get current certificate information from IPMI.

        :param token: X-Auth-Token from login
        :return: dict with certificate info or False
        """
        request_headers = {"Content-Type": "application/json", "X-Auth-Token": token}

        try:
            r = self.session.get(
                self.cert_info_url,
                headers=request_headers,
                verify=False,
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as e:
            print(f"ERROR: Error getting cert info: {e}")
            return False

        if not r.ok:
            print(f"ERROR: Failed to get cert info: {r.status_code}")
            return False

        try:
            data = r.json()
            # Parse dates - Supermicro format includes timezone that needs to be stripped
            valid_from_str = data["VaildFrom"].rstrip(
                re.split(r"\d{4}", data["VaildFrom"])[1]
            )
            valid_until_str = data["GoodTHRU"].rstrip(
                re.split(r"\d{4}", data["GoodTHRU"])[1]
            )

            valid_from = datetime.strptime(valid_from_str, r"%b %d %H:%M:%S %Y")
            valid_until = datetime.strptime(valid_until_str, r"%b %d %H:%M:%S %Y")

            return {
                "has_cert": True,
                "valid_from": valid_from,
                "valid_until": valid_until,
            }
        except (KeyError, IndexError, ValueError, json.JSONDecodeError) as e:
            print(f"ERROR: Error parsing cert info: {e}")
            self.logger.error("Error parsing cert info: %s", e)
            return False

    def upload_cert(self, key_file, cert_file, token):
        """Upload certificate to IPMI via Redfish.

        :param key_file: path to private key file
        :param cert_file: path to certificate file
        :param token: X-Auth-Token from login
        :return: bool
        """
        print(f"DEBUG: Reading certificate from {cert_file}")
        print(f"DEBUG: Reading key from {key_file}")

        with open(key_file, "rb") as fh:
            key_data = fh.read()

        with open(cert_file, "rb") as fh:
            cert_data = fh.read()
            # Extract certificates only (IPMI doesn't like DH PARAMS)
            cert_data = (
                b"\n".join(
                    re.findall(
                        b"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
                        cert_data,
                        re.DOTALL,
                    )
                )
                + b"\n"
            )

        # For Redfish, only send the server certificate, not the full chain
        substr = b"-----END CERTIFICATE-----\n"
        cert_only = cert_data.split(substr, maxsplit=1)[0] + substr

        print(f"DEBUG: Certificate data length: {len(cert_data)} bytes")
        print(f"DEBUG: Server cert only length: {len(cert_only)} bytes")
        print(f"DEBUG: Key data length: {len(key_data)} bytes")

        # Use dict format for multipart file upload
        files_to_upload = {
            "cert_file": ("cert.pem", cert_only, "application/octet-stream"),
            "key_file": ("key.pem", key_data, "application/octet-stream"),
        }

        request_headers = {"X-Auth-Token": token}

        print(f"DEBUG: Uploading to {self.upload_cert_url}")
        try:
            result = self.session.post(
                self.upload_cert_url,
                files=files_to_upload,
                headers=request_headers,
                timeout=REQUEST_TIMEOUT,
                verify=False,
            )
        except requests.RequestException as e:
            print(f"ERROR: Upload error: {e}")
            return False

        print(f"DEBUG: Upload response status: {result.status_code}")
        print(f"DEBUG: Upload response text: {result.text}")
        self.logger.debug("Upload response status: %s", result.status_code)
        self.logger.debug("Upload response text: %s", result.text)

        if (
            "SSL certificate and private key were successfully uploaded"
            not in result.text
        ):
            print(f"ERROR: Upload failed. Status: {result.status_code}")
            print(f"ERROR: Response: {result.text}")
            print(f"ERROR: Response headers: {result.headers}")
            return False

        print("SUCCESS: Certificate uploaded successfully!")
        return True

    def reboot_ipmi(self, token):
        """Reboot IPMI to apply certificate changes.

        :param token: X-Auth-Token from login
        :return: bool
        """
        request_headers = {"X-Auth-Token": token}

        try:
            result = self.session.post(
                self.reboot_url,
                headers=request_headers,
                timeout=REQUEST_TIMEOUT,
                verify=False,
            )
        except requests.RequestException as e:
            print(f"ERROR: Reboot error: {e}")
            return False

        if not result.ok:
            print(f"ERROR: Reboot failed: {result.status_code}")
            return False

        return True


def parse_valid_until(pem_file):
    """Parse certificate expiration date from PEM file."""
    with open(pem_file, "rb") as fh:
        cert = openssl_crypto.load_certificate(openssl_crypto.FILETYPE_PEM, fh.read())
    return datetime.strptime(cert.get_notAfter().decode("utf8"), "%Y%m%d%H%M%SZ")


def _build_arg_parser():
    """Build the argparse parser for the updater CLI."""
    parser = argparse.ArgumentParser(
        description="Update Supermicro IPMI SSL certificate (Redfish API only)"
    )
    parser.add_argument("--ipmi-url", required=True, help="Supermicro IPMI URL")
    parser.add_argument(
        "--model",
        required=True,
        help="Board model: X12, X13, or H13 (all use Redfish)",
    )
    parser.add_argument("--key-file", required=True, help="X.509 Private key filename")
    parser.add_argument("--cert-file", required=True, help="X.509 Certificate filename")
    parser.add_argument(
        "--username", required=True, help="IPMI username with admin access"
    )
    parser.add_argument("--password", required=True, help="IPMI user password")
    parser.add_argument(
        "--no-reboot",
        action="store_true",
        help="Skip IPMI reboot (manual reboot required)",
    )
    parser.add_argument(
        "--force-update",
        action="store_true",
        help="Force update even if certificate dates match",
    )
    parser.add_argument("--quiet", action="store_true", help="Minimal output")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    return parser


def _validate_args(args):
    """Validate CLI arguments and normalise the IPMI URL. Exits on failure."""
    if not os.path.isfile(args.key_file):
        print(f"ERROR: --key-file '{args.key_file}' doesn't exist!")
        sys.exit(2)
    if not os.path.isfile(args.cert_file):
        print(f"ERROR: --cert-file '{args.cert_file}' doesn't exist!")
        sys.exit(2)

    if args.ipmi_url.endswith("/"):
        args.ipmi_url = args.ipmi_url[:-1]

    if args.model.upper() not in ["X12", "X13", "H13"]:
        print(f"ERROR: Unsupported model '{args.model}'")
        print("This version only supports X12, X13, and H13 boards with Redfish API")
        sys.exit(2)

    model_display = args.model.upper()
    if args.model.upper() in ["X13", "H13"] and not args.quiet:
        print(f"Note: {args.model.upper()} uses same Redfish API as X12")
    return model_display


def _configure_debug_logging():
    """Enable verbose HTTP and requests logging."""
    http_client.HTTPConnection.debuglevel = 1
    logging.basicConfig()
    logging.getLogger().setLevel(logging.DEBUG)
    requests_log = logging.getLogger("requests.packages.urllib3")
    requests_log.setLevel(logging.DEBUG)
    requests_log.propagate = True


def _login_and_get_token(updater, args):
    """Login to IPMI and return the auth token (exits on failure)."""
    login_response = updater.login(args.username, args.password)
    if not login_response:
        print("ERROR: Login failed. Cannot continue!")
        sys.exit(2)

    try:
        return login_response.headers["X-Auth-Token"]
    except KeyError as e:
        print(f"ERROR: Failed to get auth token: {e}")
        sys.exit(2)


def _check_existing_cert(updater, token, args):
    """Compare existing cert validity against the new cert; exit 0 if no change needed."""
    cert_info = updater.get_ipmi_cert_info(token)
    if not cert_info:
        print("ERROR: Failed to get certificate information from IPMI!")
        sys.exit(2)

    current_valid_until = cert_info.get("valid_until", None)
    if not args.quiet and cert_info["has_cert"]:
        print(
            f"There exists a certificate, which is valid until: {cert_info['valid_until']}"
        )

    new_valid_until = parse_valid_until(args.cert_file)
    if current_valid_until == new_valid_until:
        if not args.force_update:
            print("New cert validity period matches existing cert, nothing to do")
            sys.exit(0)
        print("New cert validity period matches existing cert, will update regardless")


def _upload_and_verify(updater, token, args):
    """Upload the cert and verify the new validity date."""
    if not updater.upload_cert(args.key_file, args.cert_file, token):
        print("ERROR: Failed to upload certificate to IPMI!")
        sys.exit(2)

    if not args.quiet:
        print("Uploaded files ok.")

    cert_info = updater.get_ipmi_cert_info(token)
    if not cert_info:
        print("ERROR: Failed to verify certificate after upload!")
        sys.exit(2)

    if not args.quiet and cert_info["has_cert"]:
        print(f"After upload, certificate is valid until: {cert_info['valid_until']}")


def _maybe_reboot(updater, token, args):
    """Reboot the IPMI unless --no-reboot was passed."""
    if not args.no_reboot:
        if not args.quiet:
            print("Rebooting IPMI to apply changes...")
        if not updater.reboot_ipmi(token):
            print("WARNING: Reboot failed! Manual reboot may be required.")
    elif not args.quiet:
        print("Skipping reboot (manual reboot required)")


def main():
    """Entry point for the Supermicro IPMI certificate updater CLI."""
    parser = _build_arg_parser()
    args = parser.parse_args()

    model_display = _validate_args(args)

    if args.debug:
        _configure_debug_logging()

    # Disable SSL warnings (IPMI certs are often self-signed)
    requests.packages.urllib3.disable_warnings(
        requests.packages.urllib3.exceptions.InsecureRequestWarning
    )

    if not args.quiet:
        print(f"Board model is {model_display}")

    session = requests.session()
    updater = RedfishIPMIUpdater(session, args.ipmi_url)

    if args.debug:
        debug_log = logging.getLogger("RedfishIPMIUpdater")
        debug_log.setLevel(logging.DEBUG)
        updater.set_logger(debug_log)

    token = _login_and_get_token(updater, args)
    _check_existing_cert(updater, token, args)
    _upload_and_verify(updater, token, args)
    _maybe_reboot(updater, token, args)

    if not args.quiet:
        print("All done!")


if __name__ == "__main__":
    main()
