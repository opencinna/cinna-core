# Future Improvements

## User Scenarios and Features

### File based interaction with the user

User uploads file to the agent and asks to make certain actions related to that file.

User story 1:
- user uploads a list of employees in Odoo ERP
- user requests to enrich his file this their data, like add names to the list of emails 

User story 2:
- user uploads PDF file
- user asks to book expense and send expense report in Odoo ERP

Features required:
- user should be able to upload files to the conversation (or directly in the agent-env)
- user should be able to reference these files in the dialog

### User-expertise levels/roles

Certain users could not have enough expertise to build agents, but it's fine to provide them
conversational access to certain agents that were prepared for them by other users.

Features required:
- sharing agents from one user to another (one user builds, another user uses)
- copying of agents (one user builds, another user receives its own copy)

### User Artifacts

User wants to keep and reuse certain artifacts (files or even whole folders).

User story 1:
- user asks to generate reports about employees from Odoo ERP on 1 of every month
- user expects these reports to be saved in his storage (`artifacts`) for later access and archiving purpose (even if agent will be deleted later)

Features required:
- artifact's tool for the agent (CRUD) to manage artifact records on the backend, when local files of the agents could be saved in the backend DB.

### Multi-Agent UI cleanup

If user is having too many agents, and some of them are secondary, user don't want to see all of them on the main screen. 

Features required:
- user can mark in the configuration of the agent (or maybe on the agents card) that it's 'Favorite' agent, meaning visible on the main dashboard. 

### Developers Assistance Tools

User wants agent to do actions with the GitHub API.

User story:
- user gives command `check my recent PR and leave a comment about changes done in that PR` 
- agents checks-out repository given by the user and pushes back comment via API

Features required:
- ssh key setup inside the agent-env
- knowledge on how to communication with git-hub API and credentials for it
- maybe oauth credentials integrations as a GitHub app

### Files Meta-data Extraction

Agent during communication could be requested about details of a certain file, for example
agent needs to know what is the content of that PDF to make actions, like parse details
and do something with it, like book expenses.

Features required:
- agent-env tool to extract OCR data from a file and return back to the agent 

To avoid MCP tool context overload, probably it makes sense to add it to the knowledge article 
(inside container) as a prebuild script to extract data, that LLM could potentially use locally 
when it's needed, even seamlessly integrate with scripts that LLM is building for faster response.   

### Agent Communication to a User

In certain scenarios agent should be able to notify its owner of a required action in the offline mode too.

User story:
- agent faced a problem with auth access to an API and needs a new token
- agent sends a message to a user that he encountered an error

Missing features:
- maybe tool for the Agent to access
- maybe local knowledge (skill?) that LLM could call as an API and notify the user

### External Conversational Knowledge Database

In certain scenarios user would want to receive answers from the agent, for example instructions
on how to do something.

User story 1:
- user asks agent on how to configure a deal in Odoo correctly

Required features:
- connection of knowledge database that is providing details from external sources (Confluence ROVO API?)


## RANDOM UNSTRUCTURED NOTES

- instructions for building an agent that all 'destructive' or potentially dangerous actions - make a user request
- downloading files from ODOO - instructions (document analysis in general) - maybe as a separate sub-services available to the agent??
- RESTRICT users from using modifications to the agents (let only certain users to rely on building mode, in most scenarios special team members are bulding, and other teams members are using) - role on the user (user / agent builder)
