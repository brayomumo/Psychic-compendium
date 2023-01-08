package controller

import (
	"fmt"
	"log"
	"time"

	model "todo/pkg/models"
)

func AddTask(name string, description string, tasks []model.Task) []model.Task {

	// save to database
	tasks = append(tasks, *newTask(name, description))
	log.Println("Task added successfully")

	return tasks
}

func newTask(name string, description string) *model.Task {

	task := model.Task{
		Name:         name,
		Description:  description,
		Done:         false,
		DateCreated:  time.Now(),
		DateFinished: time.Time{},
	}
	return &task
}


func PrintTasks(tasks []model.Task) {
	dateFormat := "2006-02-01 12:59pm"

	// get all tasks from db

	for index, task := range tasks {
		if task.Done {
			fmt.Printf("%d: name: %s  -> Description: %s --> Done: %t  --> Date Finished: %s\n", index+1, task.Name, task.Description, task.Done, task.DateFinished.Format(dateFormat))
		} else {
			fmt.Printf("%d: name: %s  -> Description: %s --> Done: %t\n", index+1, task.Name, task.Description, task.Done)

		}
	}
}

func PrintPendingTasks(tasks []model.Task) {
	//  Get tasks with done as False from db

	for index, task := range tasks {
		if !task.Done {
			fmt.Printf("%d: name: %s  -> Description: %s --> Done: %t\n", index+1, task.Name, task.Description, task.Done)
		}
	}
}