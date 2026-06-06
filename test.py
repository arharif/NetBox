cat > Caddyfile <<'EOF'
lol {
    tls /etc/ssl/private/cert.crt /etc/ssl/private/key.key

    encode gzip

    reverse_proxy netbox:8080
}
EOF
