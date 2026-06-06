services:

  tls:
    image: caddy:2-alpine
    depends_on:
      - netbox
    volumes:
      - ./net-netbox_2025.crt:/etc/ssl/private/cert.crt:ro,z
      - ./net-netbox_2025.key:/etc/ssl/private/key.key:ro,z
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
    ports:
      - "80:80"
      - "443:443"
    restart: unless-stopped

  netbox:
    image: netbox:v4.4-3.4.1-topology
    pull_policy: never
    restart: unless-stopped

  netbox-worker:
    image: netbox:v4.4-3.4.1-topology
    pull_policy: never
    restart: unless-stopped

  postgres:
    restart: unless-stopped

  redis:
    restart: unless-stopped

  redis-cache:
    restart: unless-stopped
