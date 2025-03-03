# Auth0 Authentication Setup Guide

This guide will walk you through setting up Auth0 authentication for the Innate Simulator application.

## Prerequisites

- An Auth0 account (you can sign up for free at [auth0.com](https://auth0.com))
- The Innate Simulator codebase

## Step 1: Create an Auth0 Application

1. Log in to your Auth0 dashboard
2. Navigate to "Applications" > "Applications"
3. Click "Create Application"
4. Name your application (e.g., "Innate Simulator")
5. Select "Single Page Application" as the application type
6. Click "Create"

## Step 2: Configure Application Settings

1. In your new application settings, find the "Application URIs" section
2. Set the following values:
   - Allowed Callback URLs: `http://localhost:5173, http://localhost:8000`
   - Allowed Logout URLs: `http://localhost:5173, http://localhost:8000`
   - Allowed Web Origins: `http://localhost:5173, http://localhost:8000`
3. Scroll down and click "Save Changes"

## Step 3: Create an API

1. Navigate to "Applications" > "APIs"
2. Click "Create API"
3. Set the following values:
   - Name: "Innate Simulator API"
   - Identifier: `https://innate-simulator-api` (or any unique identifier)
   - Signing Algorithm: RS256
4. Click "Create"

## Step 4: Configure Environment Variables

1. Copy the `.env.template` file to `.env` in the `frontend` directory
2. Fill in the Auth0 configuration values:
   ```
   VITE_AUTH0_DOMAIN=your-tenant.auth0.com
   VITE_AUTH0_CLIENT_ID=your-client-id
   VITE_AUTH0_AUDIENCE=https://innate-simulator-api
   ```
   - Find your domain and client ID in the Auth0 Application settings
   - Use the API identifier you created as the audience

## Step 5: Run the Application with Auth0

1. Start the backend with Auth0 configuration:
   ```
   python main_web.py --auth0-domain your-tenant.auth0.com --auth0-audience https://innate-simulator-api
   ```

2. In a separate terminal, start the frontend:
   ```
   cd frontend
   yarn dev
   ```

## Step 6: Test Authentication

1. Open your browser to `http://localhost:5173`
2. You should see the login screen
3. Click "Log In" to authenticate with Auth0
4. After successful authentication, you'll be redirected to the simulator

## Troubleshooting

- **CORS Issues**: Ensure your Auth0 application has the correct Allowed Origins
- **Token Verification Fails**: Check that your audience and domain values match between frontend and backend
- **Login Redirect Loop**: Verify your callback URLs are correctly set in Auth0

## Development Mode

For development without Auth0:
1. Set `VITE_REQUIRE_AUTH=false` in your `.env` file
2. Run the backend without Auth0 parameters

## Additional Resources

- [Auth0 React SDK Documentation](https://auth0.com/docs/quickstart/spa/react)
- [Auth0 API Authorization](https://auth0.com/docs/get-started/authentication-and-authorization-flow/authorization-code-flow-with-pkce)
- [FastAPI with Auth0](https://auth0.com/blog/build-and-secure-fastapi-server-with-auth0/) 