#! /usr/bin/env bash

set -e
set -x

cd frontend
curl -o openapi.json http://localhost:8000/api/v1/openapi.json
npm run generate-client
npx biome format --write ./src/client
rm openapi.json
