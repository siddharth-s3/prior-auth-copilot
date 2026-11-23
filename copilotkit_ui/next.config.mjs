/** @type {import('next').NextConfig} */
export default { reactStrictMode: true, serverExternalPackages: ["@copilotkit/runtime"], typescript: { ignoreBuildErrors: true }, eslint: { ignoreDuringBuilds: true } };
