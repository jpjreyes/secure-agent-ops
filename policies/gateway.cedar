// Gateway policies (D4). Deny by default; a forbid always wins over a permit.
// Context comes from the verified token (scopes, delegation chain, on_behalf_of), from a separately signed human
// approval (approved), and from the call's actual arguments (args).

// 1. Read-only tools: any agent whose token carries telemetry:read.
@id("read-only-tools")
permit (principal, action == Action::"call", resource)
when { resource.read_only && context.scopes.contains("telemetry:read") };

// 2. Cluster actions: only the executor, only on behalf of a person (delegated through the commander), only with
//    cluster:write, only in the application namespace.
@id("executor-cluster-actions")
permit (principal == Agent::"agent:executor", action == Action::"call", resource)
when {
  resource.server == "cluster" &&
  context.delegated &&
  context.chain.contains("agent:commander") &&
  context.scopes.contains("cluster:write") &&
  context.args has namespace && context.args.namespace == "otel-demo"
};

// 3. Anything destructive needs a human approval for this exact call.
@id("destructive-needs-approval")
forbid (principal, action == Action::"call", resource)
when { resource.destructive && !context.approved };

// 4. Never touch the platform itself: the telemetry stack and the flag service's own deployment are how incidents
//    are seen and fixed (an agent "fixing" Prometheus could blind the investigation).
@id("protect-platform")
forbid (principal, action == Action::"call", resource)
when {
  resource.server == "cluster" && context.args has name &&
  ["prometheus", "jaeger", "opensearch", "otel-collector", "otel-collector-agent", "flagd"].contains(context.args.name)
};

// 5. Never scale a service to zero (that is an outage, not a remediation).
@id("no-scale-to-zero")
forbid (principal, action == Action::"call", resource == Tool::"cluster/scale_deployment")
when { context.args has replicas && context.args.replicas < 1 };
