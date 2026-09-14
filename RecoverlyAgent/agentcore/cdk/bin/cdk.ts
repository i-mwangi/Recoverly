#!/usr/bin/env node
import { AgentCoreStack, type HarnessConfig } from '../lib/cdk-stack';
import { ConfigIO, HarnessSpecSchema, type AwsDeploymentTarget } from '@aws/agentcore-cdk';
import { App, type Environment } from 'aws-cdk-lib';
import * as path from 'path';
import * as fs from 'fs';

function toEnvironment(target: AwsDeploymentTarget): Environment {
  return {
    account: target.account,
    region: target.region,
  };
}

function sanitize(name: string): string {
  return name.replace(/_/g, '-');
}

function toStackName(projectName: string, targetName: string): string {
  return `AgentCore-${sanitize(projectName)}-${sanitize(targetName)}`;
}

async function main() {
  const configRoot = path.resolve(process.cwd(), '..');
  const configIO = new ConfigIO({ baseDir: configRoot });

  const spec = await configIO.readProjectSpec();
  const targets = await configIO.readAWSDeploymentTargets();

  const specAny = spec as any;

  const mcpSpec = specAny.agentCoreGateways?.length
    ? {
        agentCoreGateways: specAny.agentCoreGateways,
        mcpRuntimeTools: specAny.mcpRuntimeTools,
        unassignedTargets: specAny.unassignedTargets,
      }
    : undefined;

  let deployedState: Record<string, unknown> | undefined;
  try {
    deployedState = JSON.parse(fs.readFileSync(path.join(configRoot, '.cli', 'deployed-state.json'), 'utf8'));
  } catch {
  }

  if (targets.length === 0) {
    throw new Error('No deployment targets configured. Please define targets in agentcore/aws-targets.json');
  }

  const projectRoot = path.resolve(configRoot, '..');

  const connectorParametersByFile: Record<string, Record<string, unknown>> = {};
  for (const kb of specAny.knowledgeBases ?? []) {
    for (const ds of kb.dataSources ?? []) {
      if (ds.type !== 'S3' && ds.connectorConfigFile) {
        const abs = path.resolve(projectRoot, ds.connectorConfigFile);
        try {
          connectorParametersByFile[ds.connectorConfigFile] = JSON.parse(fs.readFileSync(abs, 'utf-8'));
        } catch (err) {
          throw new Error(
            `Could not read connector config '${ds.connectorConfigFile}' for knowledge base '${kb.name}' at ${abs}: ${err instanceof Error ? err.message : err}`
          );
        }
      }
    }
  }

  const harnessConfigs: HarnessConfig[] = [];
  for (const entry of specAny.harnesses ?? []) {
    const harnessDir = path.resolve(projectRoot, entry.path);
    const harnessPath = path.resolve(harnessDir, 'harness.json');
    try {
      const harnessSpec = HarnessSpecSchema.parse(JSON.parse(fs.readFileSync(harnessPath, 'utf-8')));
      harnessConfigs.push({
        name: entry.name,
        executionRoleArn: harnessSpec.executionRoleArn,
        memoryName: harnessSpec.memory?.mode === 'existing' ? harnessSpec.memory.name : undefined,
        containerUri: harnessSpec.containerUri,
        hasDockerfile: !!harnessSpec.dockerfile,
        dockerfile: harnessSpec.dockerfile,
        codeLocation: harnessSpec.dockerfile ? harnessDir : undefined,
        tools: harnessSpec.tools,
        skills: harnessSpec.skills,
        apiKeyArn: harnessSpec.model?.apiKeyArn,
        efsAccessPoints: harnessSpec.efsAccessPoints,
        s3AccessPoints: harnessSpec.s3AccessPoints,
        apiFormat: harnessSpec.model?.apiFormat,
        spec: harnessSpec,
        harnessDir,
      });
    } catch (err) {
      throw new Error(
        `Could not read harness.json for "${entry.name}" at ${harnessPath}: ${err instanceof Error ? err.message : err}`
      );
    }
  }

  const app = new App();

  for (const target of targets) {
    const env = toEnvironment(target);
    const stackName = toStackName(spec.name, target.name);

    const targetState = (deployedState as Record<string, unknown>)?.targets as
      | Record<string, Record<string, unknown>>
      | undefined;
    const targetResources = targetState?.[target.name]?.resources as Record<string, unknown> | undefined;
    const credentials = targetResources?.credentials as
      | Record<string, { credentialProviderArn: string; clientSecretArn?: string }>
      | undefined;

    const paymentCredentials = credentials;

    const paymentSpec = specAny.payments?.length
      ? specAny.payments.map(
          (p: {
            name: string;
            description?: string;
            authorizerType: 'AWS_IAM' | 'CUSTOM_JWT';
            authorizerConfiguration?: unknown;
            autoPayment?: boolean;
            paymentToolAllowlist?: string[];
            networkPreferences?: string[];
            connectors: {
              name: string;
              provider?: string;
              provisionMode?: 'MANUAL' | 'QUICK_CREATE';
              credentialName?: string;
            }[];
          }) => ({
            name: p.name,
            description: p.description,
            authorizerType: p.authorizerType,
            authorizerConfiguration: p.authorizerConfiguration,
            autoPayment: p.autoPayment,
            paymentToolAllowlist: p.paymentToolAllowlist,
            networkPreferences: p.networkPreferences,
            connectors: p.connectors.map(c => {
              if (c.provisionMode === 'QUICK_CREATE') {
                return {
                  name: c.name,
                  provider: 'CoinbaseCDP' as const,
                  provisionMode: 'QUICK_CREATE' as const,
                };
              }

              if (!c.credentialName) {
                throw new Error(
                  `Manual payment connector "${c.name}" on manager "${p.name}" is missing its credential name.`
                );
              }
              const credentialProviderArn = paymentCredentials?.[c.credentialName]?.credentialProviderArn;
              if (!credentialProviderArn) {
                throw new Error(
                  `Payment connector "${c.name}" on manager "${p.name}" references credential ` +
                    `"${c.credentialName}", but no deployed credential provider was found for it. ` +
                    `Run \`agentcore deploy\` so the credential provider is created first.`
                );
              }
              return {
                name: c.name,
                provider: c.provider,
                ...(c.provisionMode && { provisionMode: c.provisionMode }),
                credentialName: c.credentialName,
                credentialProviderArn,
              };
            }),
          })
        )
      : undefined;

    new AgentCoreStack(app, stackName, {
      spec,
      mcpSpec,
      credentials,
      connectorParametersByFile,
      harnesses: harnessConfigs.length > 0 ? harnessConfigs : undefined,
      paymentSpec,
      env,
      description: `AgentCore stack for ${spec.name} deployed to ${target.name} (${target.region})`,
      tags: {
        'agentcore:project-name': spec.name,
        'agentcore:target-name': target.name,
      },
    });
  }

  app.synth();
}

main().catch((error: unknown) => {
  console.error('AgentCore CDK synthesis failed:', error instanceof Error ? error.message : error);
  process.exit(1);
});
