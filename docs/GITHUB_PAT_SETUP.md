# GitHub Personal Access Token (PAT) Setup

This document explains how to configure a GitHub Personal Access Token (PAT) for cloning private repositories in the GEML system.

## Overview

The GEML Runner API needs to clone the GigaEvolve repository to execute experiments. When the repository is private, authentication is required. This setup allows you to use a GitHub PAT for seamless authentication.

## Steps

### 1. Generate a GitHub Personal Access Token

1. Go to GitHub Settings: https://github.com/settings/tokens
2. Click "Generate new token (classic)"
3. Fill in the form:
   - **Note**: Enter a descriptive name (e.g., "GEML Development")
   - **Expiration**: Choose an appropriate expiration period
   - **Scopes**: Check the `repo` scope (this grants access to private repositories)
4. Click "Generate token"
5. **Important**: Copy the token immediately as you won't be able to see it again

### 2. Configure Environment Variables

Create a `.env` file in the project root:

```bash
# Copy the example file
cp .env.example .env
```

Edit the `.env` file with your actual values:

```bash
# GitHub Personal Access Token
GITHUB_PAT=ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# Optional: Override repository URL
GIGAVOLVE_REPO_URL=https://github.com/FusionBrainLab/gigaevo-core

# Optional: Git configuration
GIT_USER_NAME=Your Name
GIT_USER_EMAIL=your.email@example.com
```

### 3. Security Notes

- **Never commit the `.env` file** to version control
- The `.env.example` file is provided as a template
- Add `.env` to your `.gitignore` file
- Treat your PAT like a password
- Regularly rotate your tokens for security

### 4. Start the Development Environment

```bash
make dev
```

The system will now use your PAT to authenticate with GitHub and clone the private repository.

## Troubleshooting

### Authentication Issues

If you see authentication errors:

1. **Verify PAT is correct**: Ensure you copied the full token
2. **Check token permissions**: Make sure the `repo` scope is selected
3. **Token expiration**: Check if your token has expired
4. **Repository access**: Ensure your GitHub account has access to the target repository

### PAT Not Working

1. **Environment variables**: Verify the `.env` file is being loaded correctly
2. **Docker restart**: After updating the `.env` file, restart the containers:

   ```bash
   make clean
   make dev
   ```

### Repository Still Using Mock

If the system falls back to the mock repository:

1. **Check logs**: Look for authentication errors in the runner-api logs
2. **Manual test**: Try cloning manually:

   ```bash
   git clone https://YOUR_PAT@github.com/KhrulkovV/metaevolve
   ```

3. **Network issues**: Check if Docker can access GitHub

## Environment Variables Reference

| Variable             | Description                  | Required | Default                                   |
| -------------------- | ---------------------------- | -------- | ----------------------------------------- |
| `GITHUB_PAT`         | GitHub Personal Access Token | No       | None                                      |
| `GIGAVOLVE_REPO_URL` | Repository URL to clone      | No       | `https://github.com/FusionBrainLab/gigaevo-core` |
| `GIT_USER_NAME`      | Git user name for commits    | No       | `GEML Development`                        |
| `GIT_USER_EMAIL`     | Git user email for commits   | No       | `dev@geml.local`                          |

## How It Works

The system uses the PAT in multiple ways:

1. **URL embedding**: The PAT is embedded directly in the clone URL for HTTPS authentication
2. **Git configuration**: Git is configured to avoid interactive prompts
3. **Fallback**: If authentication fails, the system falls back to a mock repository for development

This approach ensures that:

- Development can continue even without authentication (using mock repo)
- Private repositories work seamlessly when PAT is provided
- No manual intervention is required during container startup
