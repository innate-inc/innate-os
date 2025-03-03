import { useAuth0 } from "@auth0/auth0-react";
import styled from "styled-components";
import { STRIPE_PAYMENT_LINK } from "../../services/authService";
import { LogoutButton } from "./LogoutButton";
import innateLogo from "../../assets/innate.png";

const Container = styled.div`
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 100vh;
  gap: 2rem;
  text-align: center;
  padding: 1rem;
  background-color: ${({ theme }) => theme.colors.background};
  color: ${({ theme }) => theme.colors.foreground};
  position: relative;
`;

const LogoContainer = styled.div`
  position: absolute;
  top: 20px;
  left: 20px;
`;

const Logo = styled.img`
  height: 40px;
  width: auto;
`;

const Title = styled.h1`
  font-size: 28px;
  font-weight: ${({ theme }) => theme.fontWeights.bold};
  margin-bottom: 1rem;
  color: ${({ theme }) => theme.colors.foreground};
`;

const Subtitle = styled.p`
  font-size: 18px;
  margin-bottom: 1rem;
  max-width: 600px;
  color: ${({ theme }) => theme.colors.muted};
  line-height: 1.5;
`;

const UserInfo = styled.div`
  padding: 1rem;
  border-radius: ${({ theme }) => theme.borderRadius};
  margin-bottom: 1rem;
  font-size: 16px;
  color: ${({ theme }) => theme.colors.foreground};
  border: 1px solid ${({ theme }) => theme.colors.border};
`;

const ButtonContainer = styled.div`
  display: flex;
  gap: 1rem;
  margin-top: 1rem;
`;

const StripeButton = styled.a`
  padding: 12px 24px;
  background-color: #6772e5;
  color: white;
  border: none;
  border-radius: ${({ theme }) => theme.borderRadius};
  font-size: 16px;
  font-family: ${({ theme }) => theme.fonts.body};
  font-weight: ${({ theme }) => theme.fontWeights.medium};
  cursor: pointer;
  text-decoration: none;
  transition: background-color 0.2s ease;

  &:hover {
    background-color: #5469d4;
  }
`;

const ContactLink = styled.a`
  color: ${({ theme }) => theme.colors.primary};
  text-decoration: underline;
  margin-top: 1rem;
  font-family: ${({ theme }) => theme.fonts.body};

  &:hover {
    text-decoration: none;
    color: ${({ theme }) => theme.colors.primaryHover};
  }
`;

export const UnauthorizedScreen = () => {
  const { user } = useAuth0();

  return (
    <Container>
      <LogoContainer>
        <Logo src={innateLogo} alt="Innate Robotics" />
      </LogoContainer>

      <Title>Access Restricted</Title>

      {user && user.email && (
        <UserInfo>
          You are signed in as <strong>{user.email}</strong>, but this account
          is not authorized.
        </UserInfo>
      )}

      <Subtitle>
        You can purchase access with a one-time payment or contact us directly.
      </Subtitle>

      <ButtonContainer>
        <StripeButton
          href={STRIPE_PAYMENT_LINK}
          target="_blank"
          rel="noopener noreferrer"
        >
          Get access now
        </StripeButton>
        <LogoutButton />
      </ButtonContainer>

      <ContactLink href="mailto:axel@innate.bot">
        Contact the founders for access
      </ContactLink>
    </Container>
  );
};
