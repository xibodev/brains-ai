FROM mcr.microsoft.com/devcontainers/python:1-3.12-bookworm

# Workaround: the base image pre-configures the yarn debian repo
# (/etc/apt/sources.list.d/yarn.list) whose GPG signing key
# (NO_PUBKEY 62D54FD4003F6525) was rotated upstream. Until the base
# image is rebuilt, any subsequent `apt-get install` — including the
# install of devcontainer features such as sshd — fails with exit
# code 100 because apt treats the unverifiable repo as fatal. Purge
# the broken repo here so feature installs run cleanly on top.
RUN rm -f /etc/apt/sources.list.d/yarn.list /etc/apt/sources.list.d/yarn.sources \
 && rm -f /etc/apt/trusted.gpg.d/yarn* /usr/share/keyrings/yarn* \
 && apt-get update \
 && rm -rf /var/lib/apt/lists/*
