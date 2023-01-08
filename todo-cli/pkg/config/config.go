package config

import (
	// "os"
	"todo/pkg/utils"

	"github.com/joho/godotenv"
)

// This stores configs from environment
type Config struct{
	DBUrl string

}

func LoadConfig() *Config{
	// load env from .env
	err := godotenv.Load()

	utils.HandleError(err)

	config :=  Config{
		DBUrl: "connect Url",
	}

	return &config
}