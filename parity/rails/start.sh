#!/usr/bin/env bash
# Container entrypoint: boots the Rails app from a private copy of the mounted source.
set -euo pipefail

NODE_VERSION=18.20.8

rm -rf /app
cp -a /src /app
cd /app
rm -rf tmp/pids log/*.log
# database.yml targets localhost; in this copy, point every environment at the db container.
sed -i 's/host: localhost/host: db/' config/database.yml

# ExecJS (pulled in by uglifier) needs a JavaScript runtime just to boot.
if [ ! -x /opt/node/bin/node ]; then
  arch=$(uname -m | sed -e 's/aarch64/arm64/' -e 's/x86_64/x64/')
  curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${arch}.tar.gz" \
    | tar -xz -C /opt/node --strip-components=1
fi
export PATH="/opt/node/bin:$PATH"

gem list -i bundler -v 2.1.4 >/dev/null || gem install bundler:2.1.4
bundle _2.1.4_ install --jobs 4

bundle _2.1.4_ exec rails db:create
if ! bundle _2.1.4_ exec rails runner 'exit(ActiveRecord::Base.connection.table_exists?(:venues) ? 0 : 1)'; then
  bundle _2.1.4_ exec rails db:schema:load
fi

exec bundle _2.1.4_ exec rails server -b 0.0.0.0 -p 3000
