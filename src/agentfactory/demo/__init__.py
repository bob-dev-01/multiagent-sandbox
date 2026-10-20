"""The defence demo: the factory run live, watched in a browser.

`app` serves the HTTP API the demo page talks to; `session` runs one task at a
time through the real system (AutoGen team, pipeline, sandbox pods, judge) and
turns everything that happens into a stream of demo events; `infra` starts and
stops the cloud resources; `autostop` is the job that stops an idle cluster.
"""
