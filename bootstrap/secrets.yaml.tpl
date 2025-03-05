---
apiVersion: v1
kind: Secret
metadata:
  name: onepassword-secret
  namespace: external-secrets
stringData:
  1password-credentials.json: op://Talos/1password/OP_CONNECT_JSON
  token: op://Talos/1password/OP_CONNECT_TOKEN
