module.exports = {
  apps: [
    {
      name: "scar-tiktok",
      script: "app.py",
      interpreter: "/home/web/tik/.venv/bin/python",
      cwd: "/home/web/tik",
      instances: 1,
      autorestart: true,
      watch: false,
      max_memory_restart: "800M",
      env: {
        HOST: "0.0.0.0",
        PORT: "5050",
        PYTHONUNBUFFERED: "1",
      },
    },
  ],
};
