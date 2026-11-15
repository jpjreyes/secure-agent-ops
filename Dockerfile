# One image, two Lambda handlers: the public API (sao.demo.app.handler) and the run worker
# (sao.demo.worker.handler). AWS base image so SnapStart works on the container (Python 3.12+).
FROM public.ecr.aws/lambda/python:3.12

COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir -r /tmp/requirements.lock && rm /tmp/requirements.lock

# Source stays at /var/task/src so paths relative to the repo root (policies, registry, data) resolve the same as
# locally; a .pth file puts it on every interpreter's path (the MCP servers run as subprocesses with a clean env).
COPY src/ ${LAMBDA_TASK_ROOT}/src/
COPY policies/ ${LAMBDA_TASK_ROOT}/policies/
COPY infra/identity/registry.yaml ${LAMBDA_TASK_ROOT}/infra/identity/registry.yaml
RUN echo "${LAMBDA_TASK_ROOT}/src" > /var/lang/lib/python3.12/site-packages/sao.pth

# The recorded incidents the demo offers (ITBench-AA dev scenarios) and their ground truth.
COPY data/itbench-aa/snapshots/Scenario-9/  ${LAMBDA_TASK_ROOT}/data/itbench-aa/snapshots/Scenario-9/
COPY data/itbench-aa/snapshots/Scenario-21/ ${LAMBDA_TASK_ROOT}/data/itbench-aa/snapshots/Scenario-21/
COPY data/itbench-aa/snapshots/Scenario-24/ ${LAMBDA_TASK_ROOT}/data/itbench-aa/snapshots/Scenario-24/
COPY data/itbench-aa/truth/Scenario-9.yaml data/itbench-aa/truth/Scenario-21.yaml \
     data/itbench-aa/truth/Scenario-24.yaml ${LAMBDA_TASK_ROOT}/data/itbench-aa/truth/

ENV SAO_IDENTITY_KEYS=/tmp/sao-identity \
    SAO_DEMO_WORK=/tmp/sao-work \
    PYTHONDONTWRITEBYTECODE=1
CMD ["sao.demo.app.handler"]
