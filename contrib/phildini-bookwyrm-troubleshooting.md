This document is a space for us to record any problems we've hit, especially ones we've hit more than once, that don't necessarily seem like they need to go into everybody's documentation.

# Tests pass, but build fails
Check that the versions we're using in the tests align with the actual versions in the build. This comes up most frequently when incrementing Python versions. 

# OpenTelemetry Dependency Version Resolution Spaghetti
There are two groups of OpenTelemetry dependencies with separate versioning strategies, but within each group the version should match or the tests will tell you all about every OpenTelemetry version that currently exists anywhere in our stuff.

You want the same version for `opentelemetry-api`, `opentelemetry-exporter-otlp-proto-grpc`, and `opentelemetry-sdk`. You also want the same version for `opentelemetry-instrumentation-celery`, `opentelemetry-instrumentation-django`, and `opentelemetry-instrumentation-psycopg2`. 
