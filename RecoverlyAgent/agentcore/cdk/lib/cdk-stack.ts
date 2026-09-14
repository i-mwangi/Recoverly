import {
  AgentCoreApplication,
  AgentCoreMcp,
  AgentCorePaymentManager,
  AgentCorePaymentConnector,
  type AgentCoreProjectSpec,
  type AgentCoreMcpSpec,
  type CustomJWTAuthorizerConfig,
  type HarnessDeploymentConfig,
} from '@aws/agentcore-cdk';
import { CfnOutput, Stack, type StackProps } from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';

export type HarnessConfig = HarnessDeploymentConfig;

export interface ManualPaymentConnectorSpec {
  name: string;
  provider: 'CoinbaseCDP' | 'StripePrivy';
  provisionMode?: 'MANUAL';
  credentialName: string;
  credentialProviderArn: string;
}

export interface QuickCreatePaymentConnectorSpec {
  name: string;
  provider: 'CoinbaseCDP';
  provisionMode: 'QUICK_CREATE';
  credentialName?: never;
  credentialProviderArn?: never;
}

export type PaymentConnectorSpec = ManualPaymentConnectorSpec | QuickCreatePaymentConnectorSpec;

export interface PaymentSpec {
  name: string;
  description?: string;
  authorizerType: 'AWS_IAM' | 'CUSTOM_JWT';
  authorizerConfiguration?: { customJWTAuthorizer: CustomJWTAuthorizerConfig };
  autoPayment?: boolean;
  paymentToolAllowlist?: string[];
  networkPreferences?: string[];
  connectors: PaymentConnectorSpec[];
}

export interface AgentCoreStackProps extends StackProps {
  spec: AgentCoreProjectSpec;
  mcpSpec?: AgentCoreMcpSpec;
  credentials?: Record<string, { credentialProviderArn: string; clientSecretArn?: string }>;
  harnesses?: HarnessConfig[];
  connectorParametersByFile?: Record<string, Record<string, unknown>>;
  paymentSpec?: PaymentSpec[];
}

function toCdkId(name: string): string {
  return name.replace(/_/g, '');
}

function isPaymentEligibleAgent(agent: { entrypoint?: string; protocol?: string }): boolean {
  if (agent.protocol && agent.protocol !== 'HTTP') {
    return false;
  }
  const entrypoint = typeof agent.entrypoint === 'string' ? agent.entrypoint : '';
  const entrypointFile = entrypoint.split(':')[0] ?? '';
  return entrypointFile.endsWith('.py');
}

export class AgentCoreStack extends Stack {
  public readonly application: AgentCoreApplication;

  constructor(scope: Construct, id: string, props: AgentCoreStackProps) {
    super(scope, id, props);

    const { spec, mcpSpec, credentials, harnesses, connectorParametersByFile, paymentSpec } = props;

    const appProps: Record<string, unknown> = { spec };
    if (harnesses?.length) {
      appProps.harnesses = harnesses;
    }
    if (connectorParametersByFile && Object.keys(connectorParametersByFile).length > 0) {
      appProps.connectorParametersByFile = connectorParametersByFile;
    }
    if (credentials) {
      appProps.credentials = credentials;
    }
    this.application = new AgentCoreApplication(this, 'Application', appProps as any);

    if (mcpSpec?.agentCoreGateways && mcpSpec.agentCoreGateways.length > 0) {
      new AgentCoreMcp(this, 'Mcp', {
        projectName: spec.name,
        mcpSpec,
        agentCoreApplication: this.application,
        credentials,
        projectTags: spec.tags,
      });
    }

    if (paymentSpec && paymentSpec.length > 0) {
      for (const payment of paymentSpec) {
        const mgrId = toCdkId(payment.name);
        const manager = new AgentCorePaymentManager(this, `Payment${mgrId}`, {
          projectName: spec.name,
          name: payment.name,
          authorizerType: payment.authorizerType,
          description: payment.description,
          authorizerConfiguration: payment.authorizerConfiguration,
          tags: spec.tags,
        });

        const prefix = `AGENTCORE_PAYMENT_${payment.name.toUpperCase().replace(/-/g, '_')}`;

        for (const env of this.application.environments.values()) {
          if (!isPaymentEligibleAgent(env.agent)) {
            continue;
          }
          env.runtime.addEnvironmentVariable(`${prefix}_MANAGER_ARN`, manager.paymentManagerArn);
          env.runtime.addEnvironmentVariable(`${prefix}_PROCESS_PAYMENT_ROLE_ARN`, manager.processPaymentRoleArn);

          env.runtime.role.addToPrincipalPolicy(
            new iam.PolicyStatement({
              actions: ['sts:AssumeRole'],
              resources: [manager.processPaymentRoleArn],
            })
          );

          env.runtime.role.addToPrincipalPolicy(
            new iam.PolicyStatement({
              actions: [
                'bedrock-agentcore:GetPaymentInstrument',
                'bedrock-agentcore:ListPaymentInstruments',
                'bedrock-agentcore:GetPaymentInstrumentBalance',
                'bedrock-agentcore:GetPaymentSession',
                'bedrock-agentcore:ListPaymentSessions',
                'bedrock-agentcore:CreatePaymentSession',
                'bedrock-agentcore:ProcessPayment',
              ],
              resources: [manager.paymentManagerArn, `${manager.paymentManagerArn}/*`],
            })
          );

          if (payment.autoPayment !== undefined) {
            env.runtime.addEnvironmentVariable(`${prefix}_AUTO_PAYMENT`, String(payment.autoPayment));
          }
          if (payment.paymentToolAllowlist) {
            env.runtime.addEnvironmentVariable(`${prefix}_TOOL_ALLOWLIST`, payment.paymentToolAllowlist.join(','));
          }
          if (payment.networkPreferences) {
            env.runtime.addEnvironmentVariable(`${prefix}_NETWORK_PREFERENCES`, payment.networkPreferences.join(','));
          }
          if (payment.authorizerType === 'CUSTOM_JWT') {
            env.runtime.addEnvironmentVariable(`${prefix}_AUTH_MODE`, 'bearer');
          }
        }

        for (const connector of payment.connectors) {
          const connId = toCdkId(connector.name);
          const schemaConnector =
            connector.provisionMode === 'QUICK_CREATE'
              ? connector
              : {
                  name: connector.name,
                  provider: connector.provider,
                  ...(connector.provisionMode && { provisionMode: connector.provisionMode }),
                  credentialName: connector.credentialName,
                };
          const compatibilityProps = {
            projectName: spec.name,
            paymentManager: manager,
            connector: schemaConnector,
            connectorName: connector.name,
            connectorType: connector.provider,
            ...(connector.provisionMode !== 'QUICK_CREATE' && {
              credentialProviderArn: connector.credentialProviderArn,
            }),
          };
          const conn = new AgentCorePaymentConnector(
            this,
            `Payment${mgrId}${connId}`,
            compatibilityProps as unknown as ConstructorParameters<typeof AgentCorePaymentConnector>[2]
          );

          if (connector === payment.connectors[0]) {
            for (const env of this.application.environments.values()) {
              if (!isPaymentEligibleAgent(env.agent)) continue;
              env.runtime.addEnvironmentVariable(`${prefix}_CONNECTOR_ID`, conn.paymentConnectorId);
            }
          }

          new CfnOutput(this, `Payment${mgrId}${connId}ConnectorId`, {
            value: conn.paymentConnectorId,
          });
          if (connector.provisionMode === 'QUICK_CREATE') {
            const quickCreateConnector = conn as AgentCorePaymentConnector & {
              paymentConnectorStatus: string;
              authorizationUrl: string;
            };
            new CfnOutput(this, `Payment${mgrId}${connId}ConnectorStatus`, {
              value: quickCreateConnector.paymentConnectorStatus,
            });
            new CfnOutput(this, `Payment${mgrId}${connId}AuthorizationUrl`, {
              value: quickCreateConnector.authorizationUrl,
            });
          }
        }

        new CfnOutput(this, `Payment${mgrId}ManagerArn`, {
          value: manager.paymentManagerArn,
        });
        new CfnOutput(this, `Payment${mgrId}ManagerId`, {
          value: manager.paymentManagerId,
        });
        new CfnOutput(this, `Payment${mgrId}ProcessPaymentRoleArn`, {
          value: manager.processPaymentRoleArn,
        });
        new CfnOutput(this, `Payment${mgrId}ResourceRetrievalRoleArn`, {
          value: manager.resourceRetrievalRoleArn,
        });
      }
    }

    new CfnOutput(this, 'StackNameOutput', {
      description: 'Name of the CloudFormation Stack',
      value: this.stackName,
    });
  }
}
