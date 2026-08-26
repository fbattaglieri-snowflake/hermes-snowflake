USE ROLE <% deploy_role %>;
USE DATABASE <% database %>;
USE SCHEMA <% schema %>;
USE WAREHOUSE <% warehouse %>;

CREATE SERVICE IF NOT EXISTS HERMES_SERVICE
  IN COMPUTE POOL <% compute_pool %>
  FROM @HERMES_CONFIG
  SPECIFICATION_TEMPLATE_FILE = 'hermes.service.yaml'
  USING (
    database => '<% database %>',
    schema => '<% schema %>',
    hermes_image_repository => '<% hermes_image_repository %>',
    image_tag => '<% image_tag %>',
    hermes_provider => '<% hermes_provider %>',
    hermes_model => '<% hermes_model %>'
  )
  AUTO_RESUME = TRUE
  MIN_INSTANCES = 1
  MAX_INSTANCES = 1
  EXTERNAL_ACCESS_INTEGRATIONS = (<% egress_integration %>)
  QUERY_WAREHOUSE = <% warehouse %>
  COMMENT = 'Hermes Agent on Snowpark Container Services';

ALTER SERVICE HERMES_SERVICE
  FROM @HERMES_CONFIG
  SPECIFICATION_TEMPLATE_FILE = 'hermes.service.yaml'
  USING (
    database => '<% database %>',
    schema => '<% schema %>',
    hermes_image_repository => '<% hermes_image_repository %>',
    image_tag => '<% image_tag %>',
    hermes_provider => '<% hermes_provider %>',
    hermes_model => '<% hermes_model %>'
  );
