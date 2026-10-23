# Tailscale ingress

The API is reachable only through Tailscale. There is no public listener.

1. Compose publishes the API on `127.0.0.1:8000` only (and Prometheus on `127.0.0.1:9090`).
2. Expose the API to your tailnet with HTTPS and a Tailscale-managed certificate:

   ```bash
   tailscale serve --bg --https=443 http://127.0.0.1:8000
   tailscale serve status
   ```

3. **Never** run `tailscale funnel` for this service: Funnel publishes to the public internet.
4. Every request still needs `Authorization: Bearer <api key>`; Tailscale is a network boundary, not
   authentication. Create keys with `docker compose run --rm api admin create-api-key --name NAME --role operator`
   (viewer keys for read-only clients). The key is printed once and only its hash is stored.
5. Metrics ports (9100–9102) are not published to the host and must not be served over Tailscale.
