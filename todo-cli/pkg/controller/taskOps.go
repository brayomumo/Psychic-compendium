package controller

import (
	"bufio"
	"fmt"
	"log"
	"time"

	model "todo/pkg/models"
	"todo/pkg/utils"
)

func AddTask(scanner bufio.Scanner, tasks []model.Task) []model.Task {
	fmt.Println("Add a new task below: ")
	fmt.Println("Task Name: ")

	scanner.Scan()
	// Scan for multiple lines, Scanln for single line
	err := scanner.Err()

	utils.HandleError(err)
	task := scanner.Text()

	fmt.Println("Task Description: ")
	scanner.Scan()

	err = scanner.Err()
	utils.HandleError(err)
	desc := scanner.Text()

	// save to database
	tasks = append(tasks, *NewTask(task, desc))
	log.Println("Task added successfully")

	return tasks
}

func NewTask(name string, description string) *model.Task {

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