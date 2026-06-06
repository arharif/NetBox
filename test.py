cat > Caddyfile <<'EOF'
net-netbox.oecd.org net-netbox-a.oecd.org net-netbox-b.oecd.org dkinetbox.oecd.org 10.102.4.81 10.102.4.82 {
    tls /etc/ssl/private/cert.crt /etc/ssl/private/key.key

    encode gzip

    reverse_proxy netbox:8080
}
EOF
