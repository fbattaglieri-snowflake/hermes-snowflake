USE ROLE <% deploy_role %>;

SHOW SERVICE CONTAINERS IN SERVICE <% database %>.<% schema %>.HERMES_SERVICE;
SHOW ENDPOINTS IN SERVICE <% database %>.<% schema %>.HERMES_SERVICE;
