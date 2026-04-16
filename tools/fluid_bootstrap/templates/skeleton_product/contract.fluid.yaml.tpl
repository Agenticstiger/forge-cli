
fluidVersion: "0.4.0"
kind: "DataProduct"
id: "${PRODUCT_ID}"
name: "${NAME}"
domain: "${DOMAIN}"
description: "${NAME} scaffolded by fluid-bootstrap"
metadata:
  layer: "${LAYER}"
  owner:
    team: "bootstrap"
    email: "owner@example.org"
  status: "Draft"
  tags: ["${DOMAIN}", "${LAYER}"]
consumes: []
build:
  transformation:
    pattern: "embedded-sql"
    engine: "duckdb"
    properties:
      model: "./models/transform.sql"
  execution:
    trigger: { type: "manual" }
    runtime: { platform: "${PROVIDER}" }
    retries: { count: 2, delaySeconds: 60 }
exposes:
  - id: "${PRODUCT_ID_UNDERSCORE}_tbl"
    type: "table"
    location:
      format: "csv"
      properties:
        path: "runtime/out/${PRODUCT_ID_UNDERSCORE}.csv"
    schema:
      - { name: "id", type: "STRING", nullable: false }
      - { name: "value", type: "STRING", nullable: true }
      - { name: "updated_at", type: "TIMESTAMP", nullable: false }
